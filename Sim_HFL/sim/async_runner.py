"""
sim/async_runner.py
-------------------
階層型「閾値ベース非同期」FL シミュレーション実行器。

実機 experiment/async-hfl の挙動を mirror:
  - 各端末は学習完了次第 edge へ push
  - Edge は K_EDGE 件溜まり次第 集約 → central へ push
  - Central は K_CENTRAL 件溜まり次第 集約 → 配布 (global_round +1)
  - 古いモデルでの更新も MAX_STALENESS_ROUNDS 以内なら受け入れ

時間モデル:
  - 離散時間ステップ t (1 step ≈ 実機の 1 ローカルサイクル時間)
  - 各端末は学習開始時に duration_steps を N(mean, std) からサンプル
  - duration ステップ後に edge へ push
  - staleness = (push 時の global_round) - (学習開始時の global_round)

ログスキーマ:
  既存の同期 runner と互換 (aggregation_complete / edge_aggregation_complete /
  inference_result / satisfaction_summary) に加え、async 固有の以下を追加:
    - terminal_push                : 端末 → edge プッシュ (staleness 付)
    - edge_aggregation_complete    : staleness_distribution / round_id_window 拡張
    - central_aggregation_complete : K_central 集約発火
    - terminal_stale_drop          : MAX_STALENESS 超過破棄
"""
from __future__ import annotations

import copy
import json
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import torch

from .aggregator import AggregationResult, FedAvgAggregator, _fedavg_weighted
from .data_gen import APSelectionDataset, split_iid, split_non_iid, terminal_satisfaction, APP_TYPES, APP_COUNT
from . import display
from .model import APSelectionMLP
from .network_model import ap_conditions, congestion_summary
from .terminal import InferenceResult, SimTerminal, TrainingResult


class AsyncHFLRunner:
    """
    閾値ベース階層型非同期 HFL シミュレーション実行器。

    同期版 HFLRunner との違い:
      - 学習は SimTerminal.local_train を「離散ステップ後にまとめて実行」
        （duration_steps 分の待ち時間 + 実際の学習）
      - waitForServerRoundToAdvance に相当する同期点なし
      - global_round はサーバ側 (central) の集約発火ごとに +1
    """

    def __init__(self, cfg: dict, results_dir: Optional[str] = None) -> None:
        self.cfg        = cfg
        self.run_id     = str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc).isoformat()

        fed_cfg   = cfg.get("federation", {})
        model_cfg = cfg.get("model", {})
        data_cfg  = cfg.get("data", {})
        lt_cfg    = cfg.get("local_training", {})
        out_cfg   = cfg.get("output", {})
        exp_cfg   = cfg.get("experiment", {})
        async_cfg = cfg.get("async", {})

        self.num_terminals    = fed_cfg.get("num_terminals",  3)
        self.num_rounds       = fed_cfg.get("num_rounds",    30)
        self.local_rounds     = fed_cfg.get("local_rounds",   5)

        # シミュレーション総ステップ数 = num_rounds * local_rounds
        # (同期版と同じ実時間スケールで比較できるよう設定)
        self.total_steps      = self.num_rounds * self.local_rounds

        default_topology = {"edge-00": [0, 1], "edge-01": [2]}
        self.edge_topology: dict[str, list[int]] = fed_cfg.get("edge_topology", default_topology)

        self.epochs         = lt_cfg.get("epochs",              5)
        self.batch_size     = lt_cfg.get("batch_size",         16)
        self.lr             = lt_cfg.get("learning_rate",  0.0001)
        self.weight_decay   = lt_cfg.get("weight_decay",    0.01)
        self.clip_max_norm  = lt_cfg.get("clip_max_norm",    1.0)
        self.warmup_steps   = lt_cfg.get("warmup_steps",      10)
        self.min_lr_ratio   = lt_cfg.get("min_lr_ratio",     0.1)

        self.dropout_p      = model_cfg.get("dropout_p",     0.1)
        self.input_size     = model_cfg.get("input_size",       6)
        self.hidden_size    = model_cfg.get("hidden_size",     32)
        self.output_size    = model_cfg.get("output_size",      4)

        self.seed           = exp_cfg.get("seed", 42)
        self.baseline_mode  = exp_cfg.get("baseline_mode", "none")
        self.min_switch_rounds = int(exp_cfg.get("min_switch_rounds", 0))
        self.iid            = data_cfg.get("iid",           True)
        self.alpha          = data_cfg.get("dirichlet_alpha", 0.5)
        self.total_samples  = data_cfg.get("total_samples", 3000)
        self.csv_path       = data_cfg.get("csv_path")
        self.verbose        = out_cfg.get("verbose",        True)

        # ── Async パラメータ ─────────────────────────────────
        # K_edge: edge ごとの集約閾値（pending ≥ K で発火）
        # K_central: central の集約閾値（edge 由来 pending ≥ K で発火）
        # max_staleness_rounds: これ以上古い更新は破棄
        # terminal_speed_mean/std: 端末学習所要ステップの正規分布パラメータ
        self.k_edge              = int(async_cfg.get("k_edge", 2))
        self.k_central           = int(async_cfg.get("k_central", 1))
        self.max_staleness_rounds = int(async_cfg.get("max_staleness_rounds", 5))
        self.terminal_speed_mean = float(async_cfg.get("terminal_speed_mean", 1.0))
        self.terminal_speed_std  = float(async_cfg.get("terminal_speed_std", 0.3))

        # ── AP 設定 ─────────────────────────────────────────
        self.aps_cfg = data_cfg.get("aps", [])
        if not self.aps_cfg:
            self.aps_cfg = [
                data_cfg.get("ap_a", {"tp_mean": 15.0, "tp_std": 3.0, "rtt_mean": 150.0, "rtt_std": 20.0}),
                data_cfg.get("ap_b", {"tp_mean": 5.0,  "tp_std": 1.0, "rtt_mean": 30.0,  "rtt_std": 8.0})
            ]
        self.num_aps = len(self.aps_cfg)

        self.ap_tp_mean  = [ap.get("tp_mean",  15.0) for ap in self.aps_cfg]
        self.ap_tp_std   = [ap.get("tp_std",    3.0) for ap in self.aps_cfg]
        self.ap_rtt_mean = [ap.get("rtt_mean",150.0) for ap in self.aps_cfg]
        self.ap_rtt_std  = [ap.get("rtt_std",  20.0) for ap in self.aps_cfg]

        self.ap_queue_params: list[dict] = []
        for ap_cfg in self.aps_cfg:
            self.ap_queue_params.append({
                "mu_rtt":        ap_cfg.get("mu_rtt"),
                "base_rtt_ms":   ap_cfg.get("rtt_mean", 100.0),
                "n_channels":    ap_cfg.get("n_channels",    2),
                "tp_init_mbps":  ap_cfg.get("tp_mean",   50.0),
                "rtt_noise_std": ap_cfg.get("rtt_noise_std", 2.0),
                "tp_noise_std":  ap_cfg.get("tp_noise_std",  1.0),
            })
        self.use_queue_model: bool = (
            len(self.ap_queue_params) > 0 and self.ap_queue_params[0]["mu_rtt"] is not None
        )

        # ── 結果出力先 ────────────────────────────────────
        base_dir = results_dir or out_cfg.get("results_dir", "results")
        self.results_dir = Path(base_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.round_log_path = self.results_dir / "logs" / f"rounds_{ts}_{self.run_id[:8]}.jsonl"
        self.round_log_path.parent.mkdir(parents=True, exist_ok=True)

        # 端末→edge_id の逆引き
        self._term_to_edge: dict[int, str] = {}
        for edge_id, tidx_list in self.edge_topology.items():
            for tidx in tidx_list:
                self._term_to_edge[tidx] = edge_id

    # ------------------------------------------------------------------ #
    # メイン実行
    # ------------------------------------------------------------------ #
    def run(self) -> list[AggregationResult]:
        self._log_event(
            f"run_id={self.run_id}  mode=async  terminals={self.num_terminals}  "
            f"edges={len(self.edge_topology)}  total_steps={self.total_steps}  "
            f"K_edge={self.k_edge}  K_central={self.k_central}  "
            f"max_staleness={self.max_staleness_rounds}  "
            f"speed_mean={self.terminal_speed_mean}  speed_std={self.terminal_speed_std}  "
            f"epochs/round={self.epochs}  iid={self.iid}  lr={self.lr}  "
            f"csv={self.csv_path or 'default(training_data_list/default)'}"
        )

        if self.verbose:
            display.print_phase(1, "データ生成・分割")

        full_dataset = APSelectionDataset(
            csv_path=self.csv_path,
            n_samples=self.total_samples,
            seed=self.seed,
        )
        if self.iid:
            client_datasets = split_iid(full_dataset, self.num_terminals, seed=self.seed)
        else:
            client_datasets = split_non_iid(
                full_dataset, self.num_terminals, alpha=self.alpha, seed=self.seed
            )

        if self.verbose:
            display.print_data_split([len(d) for d in client_datasets], self.iid)

        # 端末初期化
        all_terminals: list[SimTerminal] = []
        for k in range(self.num_terminals):
            ds          = client_datasets[k]
            train_n     = max(1, int(len(ds) * 0.8))
            n_batches   = max(1, (train_n + self.batch_size - 1) // self.batch_size)
            total_steps = self.epochs * n_batches

            all_terminals.append(SimTerminal(
                terminal_id   = f"sim-term-{k:02d}",
                local_data    = ds,
                input_size    = self.input_size,
                hidden_size   = self.hidden_size,
                output_size   = self.output_size,
                dropout_p     = self.dropout_p,
                lr            = self.lr,
                weight_decay  = self.weight_decay,
                clip_max_norm = self.clip_max_norm,
                warmup_steps  = self.warmup_steps,
                total_steps   = total_steps,
                min_lr_ratio  = self.min_lr_ratio,
                batch_size    = self.batch_size,
                seed          = self.seed + k,
                min_switch_rounds = self.min_switch_rounds,
            ))

        terminal_apps_cfg = self.cfg.get("data", {}).get("terminal_apps")
        if terminal_apps_cfg is not None and len(terminal_apps_cfg) >= self.num_terminals:
            self._terminal_app_idx = [int(a) % APP_COUNT for a in terminal_apps_cfg[:self.num_terminals]]
        else:
            rng_app = np.random.default_rng(self.seed + 999)
            self._terminal_app_idx = rng_app.integers(0, APP_COUNT, self.num_terminals).tolist()

        topology: dict[str, list[SimTerminal]] = {
            edge_id: [all_terminals[i] for i in indices]
            for edge_id, indices in self.edge_topology.items()
        }

        agg_history: list[AggregationResult] = []

        if self.verbose:
            display.print_phase(2, "Async HFL 連合学習")
            display.print_hfl_topology(topology)

        # ── グローバルモデル & アグリゲーター初期化 ──────
        global_model = APSelectionMLP(
            self.input_size, self.hidden_size, self.output_size, self.dropout_p
        )
        edge_aggregators: dict[str, FedAvgAggregator] = {
            edge_id: FedAvgAggregator(
                APSelectionMLP(self.input_size, self.hidden_size, self.output_size, self.dropout_p)
            )
            for edge_id in topology
        }
        central_aggregator = FedAvgAggregator(global_model)

        # 初期グローバルモデルを配布
        for term in all_terminals:
            term.set_global_weights(global_model)
        for edge_agg_obj in edge_aggregators.values():
            edge_agg_obj.global_model.load_state_dict(
                copy.deepcopy(global_model.state_dict())
            )

        # ── 非同期状態管理 ──────────────────────────────────
        # 各端末の状態: 学習開始時の global_round, 完了予定ステップ
        current_global_round = 0   # central 集約発火ごとに +1
        rng_speed = np.random.default_rng(self.seed + 7777)

        # 初期: 全端末が step=0 で学習開始
        term_state: list[dict] = []
        for tidx, term in enumerate(all_terminals):
            duration = max(1, int(round(rng_speed.normal(
                self.terminal_speed_mean, self.terminal_speed_std
            ))))
            term_state.append({
                "training_start_round": current_global_round,
                "training_complete_step": 0 + duration,  # step 0 から開始
                "is_training": True,
            })

        # Edge ごとの pending updates: list of (state_dict, n_samples, training_result, training_start_round)
        edge_pending: dict[str, list[dict]] = {edge_id: [] for edge_id in topology}
        # Central pending: list of (edge_state_dict, total_n_samples, edge_round, edge_id)
        central_pending: list[dict] = []

        rng_baseline = np.random.default_rng(self.seed + 31337) if self.baseline_mode == "random" else None
        rng_net_master = np.random.default_rng(self.seed + 12345)

        # 累積カウンタ (端末ごと) — 修論で「集約参加回数のばらつき」を出す用
        per_term_push_count: list[int] = [0] * self.num_terminals
        staleness_log: list[int] = []      # 全 push の staleness 履歴
        last_inf_results: list[InferenceResult] = []

        # ── メインループ: 離散時間ステップ ───────────────────
        for t in range(1, self.total_steps + 1):

            # ── ① 学習完了端末を edge に push ──────────────────
            if self.baseline_mode == "none":
                for tidx, term in enumerate(all_terminals):
                    st = term_state[tidx]
                    if st["is_training"] and t >= st["training_complete_step"]:
                        # 実際にローカル学習を実行
                        # round_id は学習開始時の global_round (staleness 追跡用)
                        train_round = st["training_start_round"]
                        result = term.local_train(
                            epochs   = self.epochs,
                            run_id   = self.run_id,
                            round_id = train_round,
                        )
                        self._write_terminal_log(result)

                        # staleness 計算: 現在の global_round - 学習開始時の global_round
                        staleness = current_global_round - train_round
                        staleness_log.append(staleness)

                        if staleness > self.max_staleness_rounds:
                            # 破棄
                            self._write_stale_drop_log(
                                terminal_id = term.terminal_id,
                                step        = t,
                                staleness   = staleness,
                                train_round = train_round,
                                cur_round   = current_global_round,
                            )
                        else:
                            edge_id = self._term_to_edge[tidx]
                            edge_pending[edge_id].append({
                                "terminal_id":   term.terminal_id,
                                "state_dict":    {k: v.clone() for k, v in term.model.state_dict().items()},
                                "n_samples":     len(term.train_ds),
                                "training_result": result,
                                "train_round":   train_round,
                                "staleness":     staleness,
                            })
                            per_term_push_count[tidx] += 1
                            self._write_push_log(
                                terminal_id = term.terminal_id,
                                step        = t,
                                edge_id     = edge_id,
                                train_round = train_round,
                                cur_round   = current_global_round,
                                staleness   = staleness,
                            )

                        # 次の学習を即座に開始 (待機なし)
                        # 最新グローバルモデルを取り込んで再学習
                        # (一度 edge に push したら set_global_weights は ③ で行う)
                        st["is_training"] = False  # 一時的に idle、③ で再開

            # ── ② Edge: pending ≥ K_edge なら集約 → central に push ──
            edge_agg_results_this_step: dict[str, AggregationResult] = {}
            for edge_id in topology:
                pending = edge_pending[edge_id]
                # 端末数 ≤ K_edge なら端末数を閾値とする (auto decay)
                effective_k = min(self.k_edge, len(topology[edge_id]))
                if len(pending) >= effective_k and effective_k > 0:
                    # FedAvg
                    items = [(p["state_dict"], p["n_samples"]) for p in pending[:effective_k]]
                    consumed = pending[:effective_k]
                    edge_pending[edge_id] = pending[effective_k:]

                    new_state, valid_indices = _fedavg_weighted(items)
                    edge_agg_obj = edge_aggregators[edge_id]
                    edge_agg_obj.global_model.load_state_dict(new_state)

                    # AggregationResult を構築（既存ログスキーマと互換）
                    valid_consumed = [consumed[i] for i in valid_indices]
                    total_samples = sum(c["n_samples"] for c in valid_consumed) or 1
                    val_accs   = [c["training_result"].val_acc   for c in valid_consumed]
                    val_losses = [c["training_result"].val_loss  for c in valid_consumed]
                    train_losses = [c["training_result"].train_loss for c in valid_consumed]
                    train_accs   = [c["training_result"].train_acc  for c in valid_consumed]

                    def _wavg(vals):
                        return sum(c["n_samples"] * v for c, v in zip(valid_consumed, vals)) / total_samples

                    edge_agg_result = AggregationResult(
                        round_id           = current_global_round,
                        run_id             = self.run_id,
                        num_participants   = len(valid_consumed),
                        num_excluded       = len(consumed) - len(valid_consumed),
                        total_samples      = total_samples,
                        global_weight_norm = round(edge_agg_obj.global_model.weight_norm(), 6),
                        avg_train_loss     = round(_wavg(train_losses), 6),
                        avg_train_acc      = round(_wavg(train_accs),   6),
                        avg_val_loss       = round(_wavg(val_losses),   6),
                        avg_val_acc        = round(_wavg(val_accs),     6),
                        min_val_acc        = round(min(val_accs), 6),
                        max_val_acc        = round(max(val_accs), 6),
                        agg_duration_ms    = 0.0,
                        participating_ids  = [c["terminal_id"] for c in valid_consumed],
                        excluded_ids       = [],
                    )
                    edge_agg_results_this_step[edge_id] = edge_agg_result

                    # 拡張ログ (staleness 分布)
                    self._write_edge_async_log(
                        edge_id = edge_id,
                        step    = t,
                        agg     = edge_agg_result,
                        round_id_window     = [c["train_round"] for c in valid_consumed],
                        staleness_distribution = [c["staleness"] for c in valid_consumed],
                    )

                    # Central pending に追加
                    central_pending.append({
                        "edge_id":      edge_id,
                        "state_dict":   {k: v.clone() for k, v in edge_agg_obj.global_model.state_dict().items()},
                        "n_samples":    total_samples,
                        "edge_round":   current_global_round,
                        "edge_agg":     edge_agg_result,
                    })

            # ── ③ Central: pending ≥ K_central なら集約 → global +1 → 配布 ──
            central_agg_this_step: AggregationResult | None = None
            if len(central_pending) >= self.k_central and self.k_central > 0:
                items = [(c["state_dict"], c["n_samples"]) for c in central_pending[:self.k_central]]
                consumed = central_pending[:self.k_central]
                central_pending = central_pending[self.k_central:]

                new_state, valid_indices = _fedavg_weighted(items)
                # 実機 central_server.py:218-221 (async + sync 両方) に合わせて
                # (sd_old + sd_new) / 2 でブレンド。global の慣性を持たせる。
                sd_old = central_aggregator.global_model.state_dict()
                blended = {k: (sd_old[k] + new_state[k]) / 2 for k in sd_old}
                central_aggregator.global_model.load_state_dict(blended)
                global_model = central_aggregator.global_model
                current_global_round += 1

                valid_consumed = [consumed[i] for i in valid_indices]
                total_samples = sum(c["n_samples"] for c in valid_consumed) or 1
                src_edge_aggs = [c["edge_agg"] for c in valid_consumed]
                val_accs   = [a.avg_val_acc   for a in src_edge_aggs]
                val_losses = [a.avg_val_loss  for a in src_edge_aggs]
                train_losses = [a.avg_train_loss for a in src_edge_aggs]
                train_accs   = [a.avg_train_acc  for a in src_edge_aggs]

                def _wavg2(vals):
                    return sum(c["n_samples"] * v for c, v in zip(valid_consumed, vals)) / total_samples

                central_agg_this_step = AggregationResult(
                    round_id           = current_global_round,
                    run_id             = self.run_id,
                    num_participants   = len(valid_consumed),
                    num_excluded       = len(consumed) - len(valid_consumed),
                    total_samples      = total_samples,
                    global_weight_norm = round(global_model.weight_norm(), 6),
                    avg_train_loss     = round(_wavg2(train_losses), 6),
                    avg_train_acc      = round(_wavg2(train_accs),   6),
                    avg_val_loss       = round(_wavg2(val_losses),   6),
                    avg_val_acc        = round(_wavg2(val_accs),     6),
                    min_val_acc        = round(min(val_accs), 6),
                    max_val_acc        = round(max(val_accs), 6),
                    agg_duration_ms    = 0.0,
                    participating_ids  = [c["edge_id"] for c in valid_consumed],
                    excluded_ids       = [],
                )
                self._write_central_async_log(
                    step    = t,
                    agg     = central_agg_this_step,
                    contributing_edges = [c["edge_id"] for c in valid_consumed],
                )
                # 既存スキーマ互換の aggregation_complete も書く (cross_compare 互換のため)
                self._write_agg_log(central_agg_this_step, step=t)
                agg_history.append(central_agg_this_step)

                # 新 global を全 edge と「現在 idle の端末」に配布
                for edge_agg_obj in edge_aggregators.values():
                    edge_agg_obj.global_model.load_state_dict(
                        copy.deepcopy(global_model.state_dict())
                    )
                # 端末は次サイクル開始時に取り込むのでここでは set しない
                # ※ ④ で is_training=False のものに set_global_weights する

            # ── ④ Idle 端末に新 global 配布 + 次学習開始 ─────
            if self.baseline_mode == "none":
                for tidx, term in enumerate(all_terminals):
                    st = term_state[tidx]
                    if not st["is_training"]:
                        # 最新 global を取り込んで次の学習開始
                        term.set_global_weights(central_aggregator.global_model)
                        duration = max(1, int(round(rng_speed.normal(
                            self.terminal_speed_mean, self.terminal_speed_std
                        ))))
                        st["training_start_round"]   = current_global_round
                        st["training_complete_step"] = t + duration
                        st["is_training"]            = True

            # ── ⑤ AP 選択 + 満足度測定 (毎ステップ) ───────────
            rng_net = np.random.default_rng(self.seed + t * 100)
            n_on_ap = [0] * self.num_aps
            for term in all_terminals:
                if term._current_ap < self.num_aps:
                    n_on_ap[term._current_ap] += 1
                else:
                    n_on_ap[0] += 1

            if self.use_queue_model:
                tp_rtt_per_ap: list[tuple[float, float]] = []
                for ap_idx in range(self.num_aps):
                    p = self.ap_queue_params[ap_idx]
                    tp_val, rtt_val = ap_conditions(
                        n_connected   = n_on_ap[ap_idx],
                        mu_rtt        = p["mu_rtt"],
                        base_rtt_ms   = p["base_rtt_ms"],
                        n_channels    = p["n_channels"],
                        tp_init_mbps  = p["tp_init_mbps"],
                        rng           = rng_net,
                        rtt_noise_std = p["rtt_noise_std"],
                        tp_noise_std  = p["tp_noise_std"],
                    )
                    tp_rtt_per_ap.append((tp_val, rtt_val))
            else:
                tp_rtt_per_ap = [
                    (
                        max(0.1, float(rng_net.normal(self.ap_tp_mean[i],  self.ap_tp_std[i]))),
                        max(1.0, float(rng_net.normal(self.ap_rtt_mean[i], self.ap_rtt_std[i]))),
                    )
                    for i in range(self.num_aps)
                ]

            c_summary = congestion_summary(n_on_ap, self.ap_queue_params)
            self._write_congestion_log(t, n_on_ap, tp_rtt_per_ap, c_summary)

            inf_results: list[InferenceResult] = []
            for tidx, term in enumerate(all_terminals):
                app = self._terminal_app_idx[tidx]
                satisfactions = [terminal_satisfaction(app, tp, rtt) for tp, rtt in tp_rtt_per_ap]
                best_ap = satisfactions.index(max(satisfactions)) if satisfactions else 0
                inf = term.run_inference(
                    run_id        = self.run_id,
                    round_id      = t,
                    ap_conditions = tp_rtt_per_ap,
                    app_idx       = app,
                    best_ap       = best_ap,
                    baseline_mode = self.baseline_mode,
                    rng           = rng_baseline,
                )
                inf_results.append(inf)
            for inf in inf_results:
                self._write_inference_log(inf)

            avg_satisfaction = sum(r.satisfaction for r in inf_results) / max(len(inf_results), 1)
            self._write_satisfaction_log(t, inf_results, avg_satisfaction, n_on_ap)
            last_inf_results = inf_results

            if self.verbose and central_agg_this_step is not None:
                display.print_global_round(
                    g_rnd        = current_global_round,
                    total_rounds = self.num_rounds,
                    val_acc      = central_agg_this_step.avg_val_acc,
                    val_loss     = central_agg_this_step.avg_val_loss,
                    satisfaction = avg_satisfaction,
                    inf_results  = inf_results,
                    n_on_ap      = n_on_ap,
                    tp_rtt_ap    = tp_rtt_per_ap,
                )

        # ── 終了サマリー ───────────────────────────────────
        # agg_history が空（central 一度も発火しなかった）の場合に備えてダミー追加
        if not agg_history:
            agg_history.append(AggregationResult(
                round_id=0, run_id=self.run_id,
                num_participants=0, num_excluded=0, total_samples=0,
                global_weight_norm=0.0,
                avg_train_loss=0.0, avg_train_acc=0.0,
                avg_val_loss=0.0, avg_val_acc=0.0,
                min_val_acc=0.0, max_val_acc=0.0,
                agg_duration_ms=0.0,
                participating_ids=[], excluded_ids=[],
            ))

        # async サマリーログ
        self._write_async_summary(
            per_term_push_count = per_term_push_count,
            staleness_log       = staleness_log,
            final_global_round  = current_global_round,
        )

        self._log_event(
            f"Async HFL シミュレーション完了  "
            f"final_global_round={current_global_round}  "
            f"最終 val_acc={agg_history[-1].avg_val_acc:.4f}  "
            f"最終 avg_satisfaction={sum(r.satisfaction for r in last_inf_results) / max(len(last_inf_results), 1):.4f}"
        )
        return agg_history

    # ------------------------------------------------------------------ #
    # ログ書き出し
    # ------------------------------------------------------------------ #
    def _write_terminal_log(self, r: TrainingResult) -> None:
        event = {
            "schema_version":    1,
            "ts_ms":             int(time.time() * 1000),
            "event":             "terminal_round_complete",
            "run_id":            r.run_id,
            "terminal_id":       r.terminal_id,
            "round_id":          r.round_id,
            "train_loss":        r.train_loss,
            "train_acc":         r.train_acc,
            "val_loss":          r.val_loss,
            "val_acc":           r.val_acc,
            "test_loss":         r.test_loss,
            "test_acc":          r.test_acc,
            "n_samples":         r.n_samples,
            "epochs":            r.epochs_done,
            "weight_norm":       r.weight_norm,
            "train_duration_ms": r.train_duration_ms,
        }
        self._append_jsonl(event)

    def _write_push_log(
        self, terminal_id: str, step: int, edge_id: str,
        train_round: int, cur_round: int, staleness: int,
    ) -> None:
        self._append_jsonl({
            "schema_version": 1,
            "ts_ms":          int(time.time() * 1000),
            "event":          "terminal_push",
            "run_id":         self.run_id,
            "terminal_id":    terminal_id,
            "step":           step,
            "edge_id":        edge_id,
            "train_round":    train_round,
            "current_global_round": cur_round,
            "staleness":      staleness,
        })

    def _write_stale_drop_log(
        self, terminal_id: str, step: int, staleness: int,
        train_round: int, cur_round: int,
    ) -> None:
        self._append_jsonl({
            "schema_version": 1,
            "ts_ms":          int(time.time() * 1000),
            "event":          "terminal_stale_drop",
            "run_id":         self.run_id,
            "terminal_id":    terminal_id,
            "step":           step,
            "staleness":      staleness,
            "train_round":    train_round,
            "current_global_round": cur_round,
        })

    def _write_edge_async_log(
        self, edge_id: str, step: int, agg: AggregationResult,
        round_id_window: list[int], staleness_distribution: list[int],
    ) -> None:
        # staleness 分布をヒストグラム化
        stale_hist: dict[str, int] = {}
        for s in staleness_distribution:
            key = str(s)
            stale_hist[key] = stale_hist.get(key, 0) + 1

        self._append_jsonl({
            "schema_version":    1,
            "ts_ms":             int(time.time() * 1000),
            "event":             "edge_aggregation_complete",
            "run_id":            agg.run_id,
            "edge_id":           edge_id,
            "step":              step,
            "round_id":          agg.round_id,
            "mode":              "async",
            "k_edge":            self.k_edge,
            "num_participants":  agg.num_participants,
            "num_excluded":      agg.num_excluded,
            "total_samples":     agg.total_samples,
            "avg_val_acc":       agg.avg_val_acc,
            "avg_val_loss":      agg.avg_val_loss,
            "avg_train_loss":    agg.avg_train_loss,
            "participating_ids": agg.participating_ids,
            "round_id_window":   round_id_window,
            "staleness_distribution": stale_hist,
        })

    def _write_central_async_log(
        self, step: int, agg: AggregationResult,
        contributing_edges: list[str],
    ) -> None:
        self._append_jsonl({
            "schema_version":    1,
            "ts_ms":             int(time.time() * 1000),
            "event":             "central_aggregation_complete",
            "run_id":            agg.run_id,
            "step":              step,
            "round_id":          agg.round_id,
            "mode":              "async",
            "k_central":         self.k_central,
            "num_participants":  agg.num_participants,
            "total_samples":     agg.total_samples,
            "contributing_edges": contributing_edges,
            "avg_val_acc":       agg.avg_val_acc,
            "avg_val_loss":      agg.avg_val_loss,
            "global_weight_norm": agg.global_weight_norm,
        })

    def _write_agg_log(self, a: AggregationResult, step: int) -> None:
        # 既存 cross_compare / analysis パイプラインが期待する schema
        self._append_jsonl({
            "schema_version":     1,
            "ts_ms":              int(time.time() * 1000),
            "event":              "aggregation_complete",
            "run_id":             a.run_id,
            "round_id":           a.round_id,
            "global_round":       a.round_id,
            "local_round":        None,
            "step":               step,
            "num_participants":   a.num_participants,
            "num_excluded":       a.num_excluded,
            "total_samples":      a.total_samples,
            "global_weight_norm": a.global_weight_norm,
            "avg_train_loss":     a.avg_train_loss,
            "avg_train_acc":      a.avg_train_acc,
            "avg_val_loss":       a.avg_val_loss,
            "avg_val_acc":        a.avg_val_acc,
            "min_val_acc":        a.min_val_acc,
            "max_val_acc":        a.max_val_acc,
            "agg_duration_ms":    a.agg_duration_ms,
            "participating_ids":  a.participating_ids,
            "excluded_ids":       a.excluded_ids,
        })

    def _write_inference_log(self, r: InferenceResult) -> None:
        self._append_jsonl({
            "schema_version":      1,
            "ts_ms":               int(time.time() * 1000),
            "event":               "inference_result",
            "run_id":              r.run_id,
            "terminal_id":         r.terminal_id,
            "round_id":            r.round_id,
            "inference_acc":       r.inference_acc,
            "inference_loss":      r.inference_loss,
            "satisfaction_before": r.satisfaction_before,
            "satisfaction":        r.satisfaction,
            "current_ap":          r.current_ap,
            "predicted_ap":        r.predicted_ap,
            "switched":            r.switched,
            "predicted_tier":      r.predicted_tier,
            "ap_confidence":       r.ap_confidence,
            "avg_confidence":      r.avg_confidence,
            "n_test_samples":      r.n_test_samples,
        })

    def _write_congestion_log(
        self, step: int, n_on_ap: list[int],
        tp_rtt_per_ap: list[tuple[float, float]], summary: list[dict],
    ) -> None:
        self._append_jsonl({
            "schema_version": 1,
            "ts_ms":          int(time.time() * 1000),
            "event":          "ap_congestion",
            "run_id":         self.run_id,
            "global_round":   step,
            "n_on_ap":        n_on_ap,
            "tp_rtt_per_ap":  [{"tp_mbps": round(tp, 3), "rtt_ms": round(rtt, 2)} for tp, rtt in tp_rtt_per_ap],
            "queue_model":    self.use_queue_model,
            "ap_details":     summary,
        })

    def _write_satisfaction_log(
        self, step: int, inf_results: list[InferenceResult],
        avg_satisfaction: float, n_on_ap: list[int] | None = None,
    ) -> None:
        sats = [r.satisfaction for r in inf_results]
        sats_before = [r.satisfaction_before for r in inf_results]
        _eps = 1e-9
        try:
            harmonic_mean_sat = len(sats) / sum(1.0 / max(s, _eps) for s in sats) if sats else 0.0
        except Exception:
            harmonic_mean_sat = 0.0
        try:
            harmonic_mean_sat_before = (
                len(sats_before) / sum(1.0 / max(s, _eps) for s in sats_before)
                if sats_before else 0.0
            )
        except Exception:
            harmonic_mean_sat_before = 0.0
        avg_sat_before = sum(sats_before) / max(len(sats_before), 1) if sats_before else 0.0

        capacity_overflow_count = 0
        if n_on_ap:
            for ap_idx, n_connected in enumerate(n_on_ap):
                params = self.ap_queue_params[ap_idx] if ap_idx < len(self.ap_queue_params) else {}
                n_channels = params.get("n_channels", 2) if isinstance(params, dict) else 2
                if n_connected > n_channels:
                    capacity_overflow_count += n_connected - n_channels

        self._append_jsonl({
            "schema_version":          1,
            "ts_ms":                   int(time.time() * 1000),
            "event":                   "satisfaction_summary",
            "run_id":                  self.run_id,
            "round_id":                step,
            "avg_satisfaction":        round(avg_satisfaction, 6),
            "avg_satisfaction_before": round(avg_sat_before,   6),
            "min_satisfaction":        round(min(sats) if sats else 0.0, 6),
            "max_satisfaction":        round(max(sats) if sats else 0.0, 6),
            "harmonic_mean_satisfaction":        round(harmonic_mean_sat,        6),
            "harmonic_mean_satisfaction_before": round(harmonic_mean_sat_before, 6),
            "n_switched":              sum(1 for r in inf_results if r.switched),
            "capacity_overflow_count": capacity_overflow_count,
            "n_on_ap":                 n_on_ap,
            "terminal_details": [
                {
                    "terminal_id":         r.terminal_id,
                    "satisfaction_before": round(r.satisfaction_before, 6),
                    "satisfaction":        round(r.satisfaction,        6),
                    "current_ap":          r.current_ap,
                    "predicted_ap":        r.predicted_ap,
                    "switched":            r.switched,
                    "predicted_tier":      r.predicted_tier,
                    "app_type":            APP_TYPES[self._terminal_app_idx[i]] if i < len(self._terminal_app_idx) else "unknown",
                }
                for i, r in enumerate(inf_results)
            ],
        })

    def _write_async_summary(
        self, per_term_push_count: list[int],
        staleness_log: list[int],
        final_global_round: int,
    ) -> None:
        # 端末あたり push 回数のばらつき係数 (CV = std / mean)
        if per_term_push_count and sum(per_term_push_count) > 0:
            mean_pc = sum(per_term_push_count) / len(per_term_push_count)
            try:
                std_pc = statistics.stdev(per_term_push_count) if len(per_term_push_count) >= 2 else 0.0
            except Exception:
                std_pc = 0.0
            cv = std_pc / max(mean_pc, 1e-9)
        else:
            mean_pc = std_pc = cv = 0.0

        # staleness 統計
        if staleness_log:
            stale_mean = sum(staleness_log) / len(staleness_log)
            stale_sorted = sorted(staleness_log)
            n = len(stale_sorted)
            stale_p50 = stale_sorted[n // 2]
            stale_p95 = stale_sorted[min(n - 1, int(n * 0.95))]
            stale_max = max(staleness_log)
        else:
            stale_mean = stale_p50 = stale_p95 = stale_max = 0

        self._append_jsonl({
            "schema_version":   1,
            "ts_ms":            int(time.time() * 1000),
            "event":            "async_summary",
            "run_id":           self.run_id,
            "final_global_round": final_global_round,
            "per_terminal_push_count": per_term_push_count,
            "push_count_mean":  round(mean_pc, 3),
            "push_count_std":   round(std_pc, 3),
            "push_count_cv":    round(cv, 3),
            "staleness_mean":   round(stale_mean, 3),
            "staleness_p50":    stale_p50,
            "staleness_p95":    stale_p95,
            "staleness_max":    stale_max,
            "n_pushes":         len(staleness_log),
        })

    def _log_event(self, msg: str) -> None:
        self._append_jsonl({
            "schema_version": 1,
            "ts_ms":          int(time.time() * 1000),
            "event":          "runner_info",
            "run_id":         self.run_id,
            "msg":            msg,
        })

    def _append_jsonl(self, event: dict) -> None:
        with open(self.round_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
