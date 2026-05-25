"""
sim/runner.py
-------------
階層型フェデレーテッドラーニング (HFL) シミュレーション実行器。

実験設計（実機 TrainingViewModel.runFiveCycles と同一フロー）:
  - エッジサーバ 2 台、端末 3 台 (エッジ0→端末0,1 / エッジ1→端末2)
  - 1グローバルラウンド (= 実機の1セッション) の流れ:
      ローカルサイクル × 5 回 (runFiveCycles の cycle ループに対応):
        ① 各端末がエポック × 5 学習（performLocalTrainingSuspend に相当）
        ② エッジ集約 (FedAvg)
        ③ セントラル集約 (FedAvg) → グローバルモデル更新 (round +1)
        ④ 新グローバルモデルを全端末に配布
           （実機: waitForServerRoundToAdvance → モデルダウンロード）
      5サイクル完了後:
        ⑤ 推論 → AP選択 (DecisionEngine に相当)
        ⑥ 満足度計測 (satisfaction_before / satisfaction_after)
  - これを 30 グローバルラウンド繰り返す

満足度計算:
  推論後に simulated TP/RTT を使い TerminalSatisfaction と同一式で計算。
  低満足 (tier 0・1) → AP 切替候補  /  高満足 (tier 2・3) → AP 維持
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

from .aggregator import AggregationResult, FedAvgAggregator
from .data_gen import APSelectionDataset, split_iid, split_non_iid, terminal_satisfaction, APP_TYPES, APP_COUNT
from . import display
from .model import APSelectionMLP
from .network_model import ap_conditions, congestion_summary
from .terminal import InferenceResult, SimTerminal, TrainingResult


class HFLRunner:
    """
    階層型フェデレーテッドラーニング (HFL) シミュレーション実行器。

    使い方:
        cfg = yaml.safe_load(open("config/default.yaml"))
        runner = HFLRunner(cfg)
        runner.run()
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

        self.num_terminals    = fed_cfg.get("num_terminals",  3)
        self.num_rounds       = fed_cfg.get("num_rounds",    30)
        self.local_rounds     = fed_cfg.get("local_rounds",   5)

        # エッジトポロジー: edge_id → [端末インデックス]
        default_topology = {"edge-00": [0, 1], "edge-01": [2]}
        self.edge_topology: dict[str, list[int]] = fed_cfg.get("edge_topology", default_topology)

        # LocalTrainer.kt と対応するハイパーパラメータ（デフォルト値は実機値と一致させる）
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

        self.seed           = cfg.get("experiment", {}).get("seed", 42)
        self.iid            = data_cfg.get("iid",           True)
        self.alpha          = data_cfg.get("dirichlet_alpha", 0.5)
        self.total_samples  = data_cfg.get("total_samples", 3000)
        self.csv_path       = data_cfg.get("csv_path")
        self.use_ap_state   = data_cfg.get("use_ap_state", False)
        self.verbose        = out_cfg.get("verbose",        True)

        # ── APネットワーク特性（config/default.yaml aps）────
        self.aps_cfg = data_cfg.get("aps", [])
        if not self.aps_cfg:
            # 互換性フォールバック
            self.aps_cfg = [
                data_cfg.get("ap_a", {"tp_mean": 15.0, "tp_std": 3.0, "rtt_mean": 150.0, "rtt_std": 20.0}),
                data_cfg.get("ap_b", {"tp_mean": 5.0,  "tp_std": 1.0, "rtt_mean": 30.0,  "rtt_std": 8.0})
            ]
        
        self.num_aps = len(self.aps_cfg)

        # Gaussian fallback 用
        self.ap_tp_mean  = [ap.get("tp_mean",  15.0) for ap in self.aps_cfg]
        self.ap_tp_std   = [ap.get("tp_std",    3.0) for ap in self.aps_cfg]
        self.ap_rtt_mean = [ap.get("rtt_mean",150.0) for ap in self.aps_cfg]
        self.ap_rtt_std  = [ap.get("rtt_std",  20.0) for ap in self.aps_cfg]

        # 待ち行列モデルパラメータ
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

        # 結果出力先
        base_dir = results_dir or out_cfg.get("results_dir", "results")
        self.results_dir = Path(base_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.round_log_path = self.results_dir / "logs" / f"rounds_{ts}_{self.run_id[:8]}.jsonl"
        self.round_log_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # メイン実行
    # ------------------------------------------------------------------ #
    def run(self) -> list[AggregationResult]:
        self._log_event(
            f"run_id={self.run_id}  terminals={self.num_terminals}  "
            f"edges={len(self.edge_topology)}  "
            f"global_rounds={self.num_rounds}  local_rounds={self.local_rounds}  "
            f"epochs/local_round={self.epochs}  iid={self.iid}  "
            f"lr={self.lr}  wd={self.weight_decay}  clip={self.clip_max_norm}"
        )

        # ── Phase 1: データ生成・分割 ──────────────────────────────────
        if self.verbose:
            display.print_phase(1, "データ生成・分割")

        full_dataset = APSelectionDataset(
            csv_path=self.csv_path,
            n_samples=self.total_samples,
            seed=self.seed,
            use_ap_state=self.use_ap_state,
            num_aps=self.num_aps,
        )
        if self.iid:
            client_datasets = split_iid(full_dataset, self.num_terminals, seed=self.seed)
        else:
            client_datasets = split_non_iid(
                full_dataset, self.num_terminals, alpha=self.alpha, seed=self.seed
            )

        if self.verbose:
            display.print_data_split([len(d) for d in client_datasets], self.iid)

        # 端末初期化（LocalTrainer.kt と同一ハイパーパラメータ）
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
            ))

        # 各端末に「実行中アプリカテゴリ」をランダム割り当て（シード固定・実験全体で不変）
        rng_app = np.random.default_rng(self.seed + 999)
        self._terminal_app_idx = rng_app.integers(0, APP_COUNT, self.num_terminals).tolist()

        # エッジごとの端末リスト（インデックスから実体へ変換）
        topology: dict[str, list[SimTerminal]] = {
            edge_id: [all_terminals[i] for i in indices]
            for edge_id, indices in self.edge_topology.items()
        }

        agg_history: list[AggregationResult] = []

        # ── Phase 2: HFL ループ ────────────────────────────────────────
        if self.verbose:
            display.print_phase(2, "HFL 連合学習")
            display.print_hfl_topology(topology)

        # ── グローバルモデル & アグリゲーター初期化（全試行で引き継ぎ）──────
        # 実機: runFiveCycles() が attemptLoadDownloadedModel() でサーバーの
        # 最新モデルをロードして続きから学習するフローに対応。
        # モデルは試行をまたいで蓄積的に成長する。
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

        # 初期グローバルモデルを全端末・エッジアグリゲーターに配布
        for term in all_terminals:
            term.set_global_weights(global_model)
        for edge_agg_obj in edge_aggregators.values():
            edge_agg_obj.global_model.load_state_dict(
                copy.deepcopy(global_model.state_dict())
            )

        for g_rnd in range(1, self.num_rounds + 1):

            # ── 5ローカルサイクル（実機 runFiveCycles の cycle ループ）─────
            # 各サイクル: 学習 → エッジ集約 → セントラル集約 → グローバルモデル配布
            # （実機: performLocalTrainingSuspend → upload → waitForServerRoundToAdvance → download）
            central_agg_this_round: AggregationResult | None = None

            for l_rnd in range(1, self.local_rounds + 1):
                # 絶対ラウンド番号（ログ・スケジューラ用、1始まり）
                composite_id = (g_rnd - 1) * self.local_rounds + l_rnd

                # ① 全端末がローカル学習（現在のモデル = 前サイクルのグローバルモデル）
                edge_agg_results: dict[str, AggregationResult] = {}
                for edge_id, edge_terms in topology.items():
                    local_results: list[TrainingResult] = []
                    for term in edge_terms:
                        if self.verbose:
                            display.print_terminal_training(term.terminal_id, g_rnd)
                        result = term.local_train(
                            epochs   = self.epochs,
                            run_id   = self.run_id,
                            round_id = composite_id,
                        )
                        local_results.append(result)
                        self._write_terminal_log(result)

                    if self.verbose:
                        display.clear_terminal_line()

                    # ② エッジ集約 (FedAvg)
                    edge_agg = edge_aggregators[edge_id].aggregate(
                        local_results, round_id=composite_id, run_id=self.run_id
                    )
                    edge_agg_results[edge_id] = edge_agg
                    self._write_edge_log(edge_id, edge_agg, l_rnd, g_rnd)

                # ③ セントラル集約 (FedAvg) — 実機では各サイクル後にサーバーが実行
                central_inputs = self._make_central_inputs(
                    topology, edge_aggregators, edge_agg_results, composite_id
                )
                central_agg = central_aggregator.aggregate(
                    central_inputs, round_id=composite_id, run_id=self.run_id
                )
                global_model = central_aggregator.global_model
                self._write_agg_log(central_agg, g_rnd=g_rnd, l_rnd=l_rnd)

                # ④ 新グローバルモデルを全端末・エッジアグリゲーターに配布
                #    （実機: waitForServerRoundToAdvance → 新モデルをダウンロード）
                for term in all_terminals:
                    term.set_global_weights(global_model)
                for edge_agg_obj in edge_aggregators.values():
                    edge_agg_obj.global_model.load_state_dict(
                        copy.deepcopy(global_model.state_dict())
                    )

                central_agg_this_round = central_agg

            # 5サイクル完了後の最終集約結果を記録
            agg_history.append(central_agg_this_round)

            # ④ 推論 + AP ネットワーク条件シミュレーション
            #    実機: 端末が現在APをプローブして TP/RTT を実測し、モデルに入力。
            #    シミュレーション: 各APの接続台数に基づき M/M/1 + Erlang-B で TP/RTT を決定。
            rng_net = np.random.default_rng(self.seed + g_rnd * 100)

            # ── 現在の接続台数を集計（端末 _current_ap から）────────────
            n_on_ap = [0] * self.num_aps
            for term in all_terminals:
                if term._current_ap < self.num_aps:
                    n_on_ap[term._current_ap] += 1
                else:
                    n_on_ap[0] += 1 # fallback

            # ── 各APのTP/RTT を決定（ラウンドごとに1セット）──────────
            if self.use_queue_model:
                # 待ち行列モデル: 接続台数 → 混雑 → TP/RTT 劣化
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
                # Gaussian フォールバック（待ち行列パラメータ未設定時）
                tp_rtt_per_ap = [
                    (
                        max(0.1, float(rng_net.normal(self.ap_tp_mean[i],  self.ap_tp_std[i]))),
                        max(1.0, float(rng_net.normal(self.ap_rtt_mean[i], self.ap_rtt_std[i]))),
                    )
                    for i in range(self.num_aps)
                ]

            # 混雑サマリーをログに記録
            c_summary = congestion_summary(n_on_ap, self.ap_queue_params)
            self._write_congestion_log(g_rnd, n_on_ap, tp_rtt_per_ap, c_summary)

            # ── エッジサーバによる一括推論（実機 start.py _do_inference_snapshot と同一）──
            # 実機: エッジサーバが全端末のメトリクスを読み取り、グローバルモデルで推論
            inf_results: list[InferenceResult] = []
            inf_results = self._edge_inference(
                global_model   = global_model,
                all_terminals  = all_terminals,
                tp_rtt_per_ap  = tp_rtt_per_ap,
                n_on_ap        = n_on_ap,
                g_rnd          = g_rnd,
            )
            for inf in inf_results:
                self._write_inference_log(inf)

            # ⑤ 満足度集計
            avg_satisfaction = (
                sum(r.satisfaction for r in inf_results) / max(len(inf_results), 1)
            )
            self._write_satisfaction_log(g_rnd, inf_results, avg_satisfaction, n_on_ap)

            # 表示
            if self.verbose:
                display.print_global_round(
                    g_rnd        = g_rnd,
                    total_rounds = self.num_rounds,
                    val_acc      = central_agg.avg_val_acc,
                    val_loss     = central_agg.avg_val_loss,
                    satisfaction = avg_satisfaction,
                    inf_results  = inf_results,
                    n_on_ap      = n_on_ap,
                    tp_rtt_ap    = tp_rtt_per_ap,
                )

        self._log_event(
            f"HFL シミュレーション完了  "
            f"最終 val_acc={agg_history[-1].avg_val_acc:.4f}  "
            f"最終 avg_satisfaction={sum(r.satisfaction for r in inf_results) / max(len(inf_results), 1):.4f}"
        )
        return agg_history

    # ------------------------------------------------------------------ #
    # エッジサーバによる一括推論（実機 start.py _do_inference_snapshot に対応）
    # ------------------------------------------------------------------ #
    def _edge_inference(
        self,
        global_model:  APSelectionMLP,
        all_terminals: list[SimTerminal],
        tp_rtt_per_ap: list[tuple[float, float]],
        n_on_ap:       list[int],
        g_rnd:         int,
    ) -> list[InferenceResult]:
        """
        実機 start.py の _do_inference_snapshot() と同一:
        エッジサーバがグローバルモデルを使い全端末の推論を一括実行する。

        実機フロー:
          1. 各端末の tp_measured_mbps, rtt_measured_ms, app_type を DB から取得
          2. inp = [tp, rtt] + one_hot[:4]
          3. _forward(inp) → softmax → best_ap = argmax
          4. 結果に基づき手動で AP 切り替え

        シミュレーション:
          - tp/rtt = 端末の現在接続APの M/M/1+Erlang-B 計算値
          - グローバルモデルで一括推論（端末個別モデルは使わない）
        """
        import torch
        from .data_gen import terminal_satisfaction, APP_COUNT, APSelectionDataset

        TP_MAX  = APSelectionDataset.TP_MAX
        RTT_MAX = APSelectionDataset.RTT_MAX

        global_model.eval()
        results: list[InferenceResult] = []

        for tidx, term in enumerate(all_terminals):
            app_idx = self._terminal_app_idx[tidx]
            cur_ap  = term._current_ap
            if cur_ap >= len(tp_rtt_per_ap):
                cur_ap = 0

            # 端末が現在APで実測した TP/RTT
            ap_tp, ap_rtt = tp_rtt_per_ap[cur_ap]

            # 特徴量構築 (start.py L1106: inp = [tp, rtt] + one_hot[:4])
            tp_norm  = float(min(max(ap_tp  / TP_MAX,  0.0), 1.0))
            rtt_norm = float(min(max(ap_rtt / RTT_MAX, 0.0), 1.0))

            one_hot = [0.0] * APP_COUNT
            one_hot[app_idx] = 1.0

            base_features = [tp_norm, rtt_norm] + one_hot

            # AP状態特徴量（15次元モデルの場合）
            if self.input_size > 6:
                for a in range(len(tp_rtt_per_ap)):
                    a_tp, a_rtt = tp_rtt_per_ap[a]
                    a_n = n_on_ap[a] if a < len(n_on_ap) else 0
                    base_features.append(float(min(max(a_tp  / APSelectionDataset.AP_TP_MAX,  0.0), 1.0)))
                    base_features.append(float(min(max(a_rtt / APSelectionDataset.AP_RTT_MAX, 0.0), 1.0)))
                    base_features.append(float(min(max(a_n   / APSelectionDataset.AP_N_MAX,   0.0), 1.0)))

            feature = torch.tensor(base_features, dtype=torch.float32).unsqueeze(0)

            # グローバルモデルで推論（エッジサーバが実行）
            with torch.no_grad():
                logits     = global_model(feature)
                probs      = torch.softmax(logits, dim=1)
                predicted  = int(probs.argmax(dim=1).item())
                confidence = float(probs.max().item())

            if predicted >= len(tp_rtt_per_ap):
                predicted = 0

            switched         = predicted != cur_ap
            term._current_ap = predicted

            # 満足度計算
            satisfaction_before = terminal_satisfaction(app_idx, ap_tp, ap_rtt)
            new_tp, new_rtt     = tp_rtt_per_ap[predicted]
            satisfaction        = terminal_satisfaction(app_idx, new_tp, new_rtt)

            # テストデータでの精度計測（端末ローカルモデルのテストデータ使用）
            inference_acc = inference_loss = avg_confidence = 0.0
            n_test = 0
            if len(term.test_ds) > 0:
                from torch.utils.data import DataLoader
                loader = DataLoader(term.test_ds, batch_size=256, shuffle=False)
                criterion = torch.nn.CrossEntropyLoss()
                all_confs = []
                total_loss, total_correct, total_samples = 0.0, 0, 0
                with torch.no_grad():
                    for X, y in loader:
                        out = global_model(X)
                        p   = torch.softmax(out, dim=1)
                        loss = criterion(out, y)
                        total_loss    += loss.item() * len(y)
                        total_correct += (out.argmax(1) == y).sum().item()
                        total_samples += len(y)
                        all_confs.extend(p.max(dim=1).values.tolist())
                inference_acc  = total_correct / max(total_samples, 1)
                inference_loss = total_loss    / max(total_samples, 1)
                avg_confidence = sum(all_confs) / max(len(all_confs), 1)
                n_test = total_samples

            results.append(InferenceResult(
                terminal_id         = term.terminal_id,
                round_id            = g_rnd,
                run_id              = self.run_id,
                inference_acc       = round(inference_acc,       6),
                inference_loss      = round(inference_loss,      6),
                satisfaction_before = round(satisfaction_before,  6),
                satisfaction        = round(satisfaction,         6),
                current_ap          = cur_ap,
                predicted_ap        = predicted,
                switched            = switched,
                predicted_tier      = predicted,
                ap_confidence       = round(confidence,          6),
                avg_confidence      = round(avg_confidence,      6),
                n_test_samples      = n_test,
            ))

        global_model.train()
        return results

    # ------------------------------------------------------------------ #
    # セントラル集約用の TrainingResult 生成
    # エッジモデルを「仮想端末」として扱い、既存の FedAvgAggregator を再利用する。
    # ------------------------------------------------------------------ #
    def _make_central_inputs(
        self,
        topology:         dict[str, list[SimTerminal]],
        edge_aggregators: dict[str, FedAvgAggregator],
        edge_agg_results: dict[str, AggregationResult],
        g_rnd:            int,
    ) -> list[TrainingResult]:
        central_inputs: list[TrainingResult] = []
        for edge_id, edge_terms in topology.items():
            edge_agg    = edge_aggregators[edge_id]
            edge_result = edge_agg_results.get(edge_id)
            # 加重平均に使うサンプル数 = エッジ内全端末の学習データ合計
            n_samples = (
                edge_result.total_samples
                if edge_result
                else sum(len(t.train_ds) for t in edge_terms)
            )
            central_inputs.append(TrainingResult(
                terminal_id       = edge_id,
                round_id          = g_rnd,
                run_id            = self.run_id,
                val_acc           = edge_result.avg_val_acc   if edge_result else 0.0,
                val_loss          = edge_result.avg_val_loss  if edge_result else 0.0,
                train_acc         = edge_result.avg_train_acc if edge_result else 0.0,
                train_loss        = edge_result.avg_train_loss if edge_result else 0.0,
                n_samples         = n_samples,
                weights_f32       = edge_agg.global_model.to_f32_flat(),
            ))
        return central_inputs

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

    def _write_edge_log(
        self, edge_id: str, a: AggregationResult, local_rnd: int, g_rnd: int
    ) -> None:
        event = {
            "schema_version":    1,
            "ts_ms":             int(time.time() * 1000),
            "event":             "edge_aggregation_complete",
            "run_id":            a.run_id,
            "edge_id":           edge_id,
            "global_round":      g_rnd,
            "local_round":       local_rnd,
            "round_id":          a.round_id,
            "num_participants":  a.num_participants,
            "num_excluded":      a.num_excluded,
            "total_samples":     a.total_samples,
            "avg_val_acc":       a.avg_val_acc,
            "avg_val_loss":      a.avg_val_loss,
            "avg_train_loss":    a.avg_train_loss,
            "participating_ids": a.participating_ids,
        }
        self._append_jsonl(event)

    def _write_agg_log(
        self, a: AggregationResult, g_rnd: int | None = None, l_rnd: int | None = None
    ) -> None:
        event = {
            "schema_version":     1,
            "ts_ms":              int(time.time() * 1000),
            "event":              "aggregation_complete",
            "run_id":             a.run_id,
            "round_id":           a.round_id,   # 絶対ラウンド番号 (composite_id)
            "global_round":       g_rnd,         # セッション番号 (1〜30)
            "local_round":        l_rnd,         # サイクル内の番号 (1〜5)
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
        }
        self._append_jsonl(event)

    def _write_inference_log(self, r: InferenceResult) -> None:
        event = {
            "schema_version":      1,
            "ts_ms":               int(time.time() * 1000),
            "event":               "inference_result",
            "run_id":              r.run_id,
            "terminal_id":         r.terminal_id,
            "round_id":            r.round_id,
            "inference_acc":       r.inference_acc,
            "inference_loss":      r.inference_loss,
            "satisfaction_before": r.satisfaction_before,   # 切替前APの満足度
            "satisfaction":        r.satisfaction,           # 切替後APの満足度 (= after)
            "current_ap":          r.current_ap,
            "predicted_ap":        r.predicted_ap,
            "switched":            r.switched,
            "predicted_tier":      r.predicted_tier,
            "ap_confidence":       r.ap_confidence,
            "avg_confidence":      r.avg_confidence,
            "n_test_samples":      r.n_test_samples,
        }
        self._append_jsonl(event)

    def _write_congestion_log(
        self,
        g_rnd:         int,
        n_on_ap:       list[int],
        tp_rtt_per_ap: list[tuple[float, float]],
        summary:       list[dict],
    ) -> None:
        """AP の混雑状態と決定された TP/RTT をログに記録する。"""
        event = {
            "schema_version": 1,
            "ts_ms":          int(time.time() * 1000),
            "event":          "ap_congestion",
            "run_id":         self.run_id,
            "global_round":   g_rnd,
            "n_on_ap":        n_on_ap,
            "tp_rtt_per_ap":  [{"tp_mbps": round(tp, 3), "rtt_ms": round(rtt, 2)} for tp, rtt in tp_rtt_per_ap],
            "queue_model":    self.use_queue_model,
            "ap_details":     summary,
        }
        self._append_jsonl(event)

    def _write_satisfaction_log(
        self, g_rnd: int, inf_results: list[InferenceResult],
        avg_satisfaction: float, n_on_ap: list[int] | None = None,
    ) -> None:
        sats = [r.satisfaction for r in inf_results]
        sats_before = [r.satisfaction_before for r in inf_results]
        min_sat  = min(sats) if sats else 0.0
        max_sat  = max(sats) if sats else 0.0
        min_sat_before = min(sats_before) if sats_before else 0.0
        max_sat_before = max(sats_before) if sats_before else 0.0
        try:
            std_sat = statistics.stdev(sats) if len(sats) >= 2 else 0.0
        except Exception:
            std_sat = 0.0
        try:
            std_sat_before = statistics.stdev(sats_before) if len(sats_before) >= 2 else 0.0
        except Exception:
            std_sat_before = 0.0
        # 調和平均 (harmonic mean): 最も不満な端末を重視する評価指標
        _eps = 1e-9
        try:
            harmonic_mean_sat = len(sats) / sum(1.0 / max(s, _eps) for s in sats) if sats else 0.0
        except Exception:
            harmonic_mean_sat = 0.0
        try:
            harmonic_mean_sat_before = len(sats_before) / sum(1.0 / max(s, _eps) for s in sats_before) if sats_before else 0.0
        except Exception:
            harmonic_mean_sat_before = 0.0
        avg_sat_before = sum(sats_before) / max(len(sats_before), 1) if sats_before else 0.0
        # 容量超過端末数: 各APの接続台数が n_channels を超えた分の合計
        capacity_overflow_count = 0
        if n_on_ap:
            for ap_idx, n_connected in enumerate(n_on_ap):
                params = self.ap_queue_params[ap_idx] if ap_idx < len(self.ap_queue_params) else {}
                n_channels = params.get("n_channels", 2) if isinstance(params, dict) else 2
                if n_connected > n_channels:
                    capacity_overflow_count += n_connected - n_channels

        event = {
            "schema_version":          1,
            "ts_ms":                   int(time.time() * 1000),
            "event":                   "satisfaction_summary",
            "run_id":                  self.run_id,
            "round_id":                g_rnd,
            "avg_satisfaction":        round(avg_satisfaction, 6),
            "avg_satisfaction_before": round(avg_sat_before,   6),
            "min_satisfaction":        round(min_sat,          6),
            "max_satisfaction":        round(max_sat,          6),
            "min_satisfaction_before": round(min_sat_before,   6),
            "max_satisfaction_before": round(max_sat_before,   6),
            "satisfaction_std":        round(std_sat,          6),
            "satisfaction_before_std": round(std_sat_before,   6),
            "harmonic_mean_satisfaction": round(harmonic_mean_sat, 6),
            "harmonic_mean_satisfaction_before": round(harmonic_mean_sat_before, 6),
            "n_switched":              sum(1 for r in inf_results if r.switched),
            "capacity_overflow_count": capacity_overflow_count,
            "n_on_ap":                 n_on_ap,
            "terminal_details":  [
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
        }
        self._append_jsonl(event)

    def _log_event(self, msg: str) -> None:
        event = {
            "schema_version": 1,
            "ts_ms":          int(time.time() * 1000),
            "event":          "runner_info",
            "run_id":         self.run_id,
            "msg":            msg,
        }
        self._append_jsonl(event)

    def _append_jsonl(self, event: dict) -> None:
        with open(self.round_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


# 後方互換エイリアス
SimRunner = HFLRunner
