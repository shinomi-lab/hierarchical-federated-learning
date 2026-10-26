"""
terminal_client/protocol.py
---------------------------
仮想端末のうち、HTTP や学習から切り離せる部分（純関数）。

実機アプリ（SP1・SP3a・SP3b 以降）と同じにそろえる:
  - 特徴量: 要求 TP・RTT の z-score（state/norm_stats.json）＋アプリ one-hot 4 = 6 次元
  - 送信: /receive_terminal_weights/{terminal_id} に f32_flat（全パラメータ）
  - モード: サーバ（meta の hfl_mode）に従い、sync はラウンド進行を待つ
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

APP_COUNT = 4
INPUT_SIZE = 2 + APP_COUNT
HIDDEN_SIZE = 32
OUTPUT_SIZE = 2  # AP 数（SP3a）


@dataclass(frozen=True)
class NormStats:
    tp_mean: float
    tp_std: float
    rtt_mean: float
    rtt_std: float

    @classmethod
    def load(cls, path: str) -> "NormStats":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return cls(float(d["tp_mean"]), float(d["tp_std"]), float(d["rtt_mean"]), float(d["rtt_std"]))


def features(need_tp: float, need_rtt: float, app_idx: int, ns: NormStats) -> List[float]:
    if not 0 <= app_idx < APP_COUNT:
        raise ValueError(f"app_idx out of range: {app_idx}")
    onehot = [0.0] * APP_COUNT
    onehot[app_idx] = 1.0
    return [(need_tp - ns.tp_mean) / ns.tp_std, (need_rtt - ns.rtt_mean) / ns.rtt_std] + onehot


def load_csv_dataset(path: str, ns: NormStats):
    """教師 CSV（needTP, needRTT, appIdx, label）→ TensorDataset。"""
    import torch
    from torch.utils.data import TensorDataset

    xs, ys = [], []
    with open(path, encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row:
                continue
            xs.append(features(float(row[0]), float(row[1]), int(row[2]), ns))
            ys.append(int(row[3]))
    return TensorDataset(torch.tensor(xs, dtype=torch.float32), torch.tensor(ys, dtype=torch.int64))


def upload_form(round_id: int, base_hash: str, n_samples: int, model_id: str = "docker-terminal") -> Dict[str, str]:
    return {
        "round_id": str(int(round_id)),
        "model_id": model_id,
        "base_hash": base_hash or "",
        "n_samples": str(int(n_samples)),
        "payload_kind": "full",
        "dtype": "f32_flat",
        "input_size": str(INPUT_SIZE),
        "hidden_size": str(HIDDEN_SIZE),
        "output_size": str(OUTPUT_SIZE),
    }


def should_wait_for_round(hfl_mode: Optional[str]) -> bool:
    """sync（未指定も sync 扱い。サーバの既定）ならラウンド進行を待つ。"""
    return (hfl_mode or "sync").lower() != "async"


def trial_validity(cycles_by_terminal: Dict[str, List[dict]], num_cycles: int,
                   hfl_mode: Optional[str] = "sync") -> Tuple[bool, str]:
    """有効な試行: 全端末が全サイクル完了、配布モデルが届いている、試行内のモデル SHA が 2 種類以上。

    配布モデルの条件は sync では cycle>=2 が全て DOWNLOADED（毎回ラウンド進行を待つため）。
    async は待たないので、新モデルが来る前のサイクルが自モデル（PROVIDED）なのは正常。各端末で
    cycle>=2 に DOWNLOADED が 1 回以上あることを求める。
    """
    if not cycles_by_terminal:
        return False, "no_terminals"
    sync = should_wait_for_round(hfl_mode)
    shas = set()
    for rows in cycles_by_terminal.values():
        done = [r for r in rows if r.get("cycle") is not None]
        if len(done) < num_cycles:
            return False, "incomplete"
        later = [r for r in done if r["cycle"] >= 2]
        for r in done:
            if r.get("model_sha256"):
                shas.add(r["model_sha256"])
        downloaded = [r.get("init_source") == "DOWNLOADED" for r in later]
        if (sync and not all(downloaded)) or (not sync and not any(downloaded)):
            return False, "not_downloaded"
    if len(shas) < 2:
        return False, "single_model"
    return True, ""
