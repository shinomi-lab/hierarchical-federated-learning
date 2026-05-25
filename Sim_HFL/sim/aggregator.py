"""
sim/aggregator.py
-----------------
FedAvg 集約器（純 Python・サーバ不要）。

実機サーバ (edge_server/endpoints/aggregation.py) の
_fedavg_state_dicts_weighted と同一ロジックを実装:
  - NaN/Inf を含む更新を集約前に除外（除外判定を index で追跡）
  - float テンソル: CPU float32 加重平均
  - 非 float テンソル: 先頭の値を使用
  - 全更新が無効な場合は ValueError
"""
from __future__ import annotations

import time
import warnings
from dataclasses import dataclass
from typing import List

import torch

from .model import APSelectionMLP
from .terminal import TrainingResult


@dataclass
class AggregationResult:
    round_id:              int
    run_id:                str
    num_participants:      int
    num_excluded:          int
    total_samples:         int

    global_weight_norm:    float
    avg_train_loss:        float
    avg_train_acc:         float
    avg_val_loss:          float
    avg_val_acc:           float
    min_val_acc:           float
    max_val_acc:           float

    agg_duration_ms:       float
    participating_ids:     list
    excluded_ids:          list


def _has_nan_inf(state_dict: dict) -> bool:
    """
    実機 aggregation.py の _has_nan_inf と同一。
    float テンソルに NaN/Inf が含まれていれば True を返す。
    """
    for t in state_dict.values():
        if torch.is_floating_point(t):
            if torch.isnan(t).any() or torch.isinf(t).any():
                return True
    return False


def _fedavg_weighted(
    items: list[tuple[dict, int]],
) -> tuple[dict, list[int]]:
    """
    実機 aggregation.py の _fedavg_state_dicts_weighted と同一ロジック。

    Returns:
        (aggregated_state_dict, valid_indices)
        valid_indices: NaN/Inf を含まなかった items のインデックスリスト
    """
    if not items:
        raise ValueError("集約対象がありません")

    # NaN/Inf のないものだけ有効とし、インデックスで追跡
    valid_indices = [
        i for i, (sd, _) in enumerate(items)
        if not _has_nan_inf(sd)
    ]
    excluded_count = len(items) - len(valid_indices)
    if excluded_count > 0:
        warnings.warn(f"NaN/Inf を含む更新を {excluded_count} 件除外しました")

    if not valid_indices:
        raise ValueError("全端末の更新に NaN/Inf が含まれており、集約できません")

    valid_items = [items[i] for i in valid_indices]
    sds     = [sd for sd, _ in valid_items]
    ns      = [n  for _, n  in valid_items]
    total_n = sum(ns) or 1
    weights = [n / total_n for n in ns]
    keys    = list(sds[0].keys())

    out: dict[str, torch.Tensor] = {}
    for k in keys:
        first = sds[0][k]
        if torch.is_floating_point(first):
            acc = None
            for i, sd in enumerate(sds):
                t = sd[k].detach()
                if t.device.type != "cpu":
                    t = t.to("cpu")
                if t.dtype != torch.float32:
                    t = t.to(torch.float32)
                if acc is None:
                    acc = t.mul(weights[i])
                else:
                    acc.add_(t.mul(weights[i]))
            out[k] = acc
        else:
            out[k] = first.detach().to("cpu")

    return out, valid_indices


class FedAvgAggregator:
    """実機サーバと同一ロジックの FedAvg 集約器"""

    def __init__(self, global_model: APSelectionMLP) -> None:
        self.global_model = global_model

    def aggregate(
        self,
        results:  List[TrainingResult],
        round_id: int,
        run_id:   str,
    ) -> AggregationResult:
        t0 = time.monotonic()

        # global_model からモデルサイズを取得して from_f32_flat に渡す
        gm = self.global_model
        _in  = gm.layer1.in_features
        _hid = gm.layer1.out_features
        _out = gm.layer3.out_features
        _dp  = gm.dropout1.p

        items = [
            (APSelectionMLP.from_f32_flat(r.weights_f32, _in, _hid, _out, _dp).state_dict(), r.n_samples)
            for r in results
        ]

        new_state, valid_indices = _fedavg_weighted(items)

        # valid_indices でどの TrainingResult が有効かを正確に特定
        valid_results   = [results[i] for i in valid_indices]
        excluded_results = [results[i] for i in range(len(results)) if i not in valid_indices]

        self.global_model.load_state_dict(new_state)
        agg_duration_ms = (time.monotonic() - t0) * 1000

        total_samples = sum(r.n_samples for r in valid_results) or 1

        def _wavg(vals: list[float]) -> float:
            return sum(r.n_samples * v for r, v in zip(valid_results, vals)) / total_samples

        val_accs     = [r.val_acc    for r in valid_results]
        val_losses   = [r.val_loss   for r in valid_results]
        train_losses = [r.train_loss for r in valid_results]
        train_accs   = [r.train_acc  for r in valid_results]

        return AggregationResult(
            round_id           = round_id,
            run_id             = run_id,
            num_participants   = len(valid_results),
            num_excluded       = len(excluded_results),
            total_samples      = total_samples,
            global_weight_norm = round(self.global_model.weight_norm(), 6),
            avg_train_loss     = round(_wavg(train_losses), 6),
            avg_train_acc      = round(_wavg(train_accs),   6),
            avg_val_loss       = round(_wavg(val_losses),   6),
            avg_val_acc        = round(_wavg(val_accs),     6),
            min_val_acc        = round(min(val_accs),        6),
            max_val_acc        = round(max(val_accs),        6),
            agg_duration_ms    = round(agg_duration_ms, 2),
            participating_ids  = [r.terminal_id for r in valid_results],
            excluded_ids       = [r.terminal_id for r in excluded_results],
        )
