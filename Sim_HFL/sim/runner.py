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
from .coordinator import central_coordinator
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
        # ベースラインモード (実機 AppConfig.BASELINE_MODE と対称)
        # "none" 以外のときは学習・集約をスキップして AP 選択+満足度測定のみ実行
        self.baseline_mode  = cfg.get("experiment", {}).get("baseline_mode", "none")
        # AP 選択の意思決定モード (CTDE フェーズ1):
        #   "independent" = 各端末が自分のモデル/baseline で独立決定 (従来動作)
        #   "coordinated" = 中央協調器が全体HM最大の joint 割当を計算し端末に強制
        # coordinated でも学習・集約は独立HFLのまま(意思決定=control-plane のみ差替え)。
        self.decision_mode  = cfg.get("experiment", {}).get("decision_mode", "independent")
        # 混雑の測り直し: 意思決定"後"の接続台数で tp/rtt を再計算して満足度を測る。
        #   False(既定) = 従来動作(意思決定"前"の接続台数で測定)。
        #   True        = 移動政策(greedy/independent)の混雑を正しく計上する公平な測定。
        # 既定を False にして既存の再現性を保つ。
        self.settle_congestion = bool(cfg.get("experiment", {}).get("settle_congestion", False))
        # 背景負荷 (時間変動): 4端末以外の外部ユーザが各APに乗る負荷を模擬。
        #   None(既定) = 背景負荷なし(従来動作)。
        #   {"hi": 台数, "dwell": 保持サイクル} = dwell ごとに1つのAPへ hi 台の背景負荷(エピソード的・非対称)。
        # これにより最適APが時間で変わり、静的モデルでは追随できず中央協調が効く regime を作る。
        self.bg_load_cfg = cfg.get("experiment", {}).get("background_load")
        # Ablation flags (慣性メカニズム切り分け):
        #   central_blend=True (default): 集約後に (sd_old + sd_new)/2 で blend
        #     (旧モノリス central_server.py 由来)
        #   terminal_continuation=True: cycle 1 のみ global を採用、cycle 2-5 は
        #     端末ローカル継続 (実機 runTrainingCycles 準拠)
        self.central_blend = bool(cfg.get("experiment", {}).get("central_blend", True))
        self.terminal_continuation = bool(cfg.get("experiment", {}).get("terminal_continuation", False))
        # AP 切替クールダウン (実機 DecisionEngine.minSwitchSeconds=300 相当をラウンド換算)
        # 0 = 無効。HFL モードのみ適用、ベースラインは実機と同様 cooldown 適用なし。
        self.min_switch_rounds = int(cfg.get("experiment", {}).get("min_switch_rounds", 0))
        self.iid            = data_cfg.get("iid",           True)
        self.alpha          = data_cfg.get("dirichlet_alpha", 0.5)
        self.total_samples  = data_cfg.get("total_samples", 3000)
        self.csv_path       = data_cfg.get("csv_path")
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
            f"lr={self.lr}  wd={self.weight_decay}  clip={self.clip_max_norm}  "
            f"csv={self.csv_path or 'default(training_data_list/default)'}"
        )

        # ── Phase 1: データ生成・分割 ──────────────────────────────────
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
                min_switch_rounds = self.min_switch_rounds,
            ))

        # 各端末に「実行中アプリカテゴリ」を割り当て（実験全体で不変）。
        # yaml で data.terminal_apps が指定されていればそれを使い、なければ seed+999 の乱数で生成。
        # 拮抗構成 (browser/video/call/other 各1台) を再現するには
        #   data.terminal_apps: [0, 1, 2, 3]
        # と yaml に書く。
        terminal_apps_cfg = self.cfg.get("data", {}).get("terminal_apps")
        if terminal_apps_cfg is not None and len(terminal_apps_cfg) >= self.num_terminals:
            self._terminal_app_idx = [int(a) % APP_COUNT for a in terminal_apps_cfg[:self.num_terminals]]
        else:
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

        # ベースライン用 RNG (random モードでの一様乱択に使用)
        rng_baseline = np.random.default_rng(self.seed + 31337) if self.baseline_mode == "random" else None

        for g_rnd in range(1, self.num_rounds + 1):

            # ── 5ローカルサイクル（実機 runFiveCycles の cycle ループ）─────
            # 各サイクル: 学習 → エッジ集約 → セントラル集約 → グローバルモデル配布
            # （実機: performLocalTrainingSuspend → upload → waitForServerRoundToAdvance → download）
            central_agg_this_round: AggregationResult | None = None

            # ── ベースラインモード: 学習・集約をスキップ ──────────────────
            # 実機 TrainingViewModel.runFiveCycles の if (BASELINE_MODE != "none") 分岐に対応。
            # ダミー AggregationResult を入れて agg_history の構造を保つ
            # (理論値比較や分析パイプラインが落ちないように)。
            if self.baseline_mode != "none":
                central_agg_this_round = AggregationResult(
                    round_id           = g_rnd,
                    run_id             = self.run_id,
                    num_participants   = 0,
                    num_excluded       = 0,
                    total_samples      = 0,
                    global_weight_norm = 0.0,
                    avg_train_loss     = 0.0,
                    avg_train_acc      = 0.0,
                    avg_val_loss       = 0.0,
                    avg_val_acc        = 0.0,
                    min_val_acc        = 0.0,
                    max_val_acc        = 0.0,
                    agg_duration_ms    = 0.0,
                    participating_ids  = [],
                    excluded_ids       = [],
                )

            for l_rnd in range(1, self.local_rounds + 1):
                # 絶対ラウンド番号（ログ・スケジューラ用、1始まり）
                # 実機 runFiveCycles の cycle 単位に対応 (1G あたり 5 サイクル)
                composite_id = (g_rnd - 1) * self.local_rounds + l_rnd

                # ── ① 学習 + 集約 (ベースラインモードはスキップ) ───────
                if self.baseline_mode == "none":
                    # 全端末がローカル学習
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

                        # エッジ集約 (FedAvg)
                        edge_agg = edge_aggregators[edge_id].aggregate(
                            local_results, round_id=composite_id, run_id=self.run_id
                        )
                        edge_agg_results[edge_id] = edge_agg
                        self._write_edge_log(edge_id, edge_agg, l_rnd, g_rnd)

                    # セントラル集約 (FedAvg)
                    # central_blend=True (デフォルト): 旧モノリス central_server.py:218-221
                    #   の (sd_old + sd_new) / 2 blend を再現。
                    # central_blend=False: 純 FedAvg (実機モジュラー central_server.main:app の
                    #   実挙動)。
                    if self.central_blend:
                        sd_old = copy.deepcopy(central_aggregator.global_model.state_dict())
                    central_inputs = self._make_central_inputs(
                        topology, edge_aggregators, edge_agg_results, composite_id
                    )
                    central_agg = central_aggregator.aggregate(
                        central_inputs, round_id=composite_id, run_id=self.run_id
                    )
                    if self.central_blend:
                        sd_new = central_aggregator.global_model.state_dict()
                        blended = {k: (sd_old[k] + sd_new[k]) / 2 for k in sd_old}
                        central_aggregator.global_model.load_state_dict(blended)
                    global_model = central_aggregator.global_model
                    self._write_agg_log(central_agg, g_rnd=g_rnd, l_rnd=l_rnd)

                    # 新グローバルモデルを全端末・エッジアグリゲーターに配布
                    # terminal_continuation=True の場合は cycle 1 のみ端末に配布し、
                    # cycle 2-5 は端末ローカル継続 (実機 runTrainingCycles 準拠)。
                    # Edge aggregator は集約のたびに global を取り直すため毎サイクル更新。
                    distribute_to_terminals = (not self.terminal_continuation) or (l_rnd == 1)
                    if distribute_to_terminals:
                        for term in all_terminals:
                            term.set_global_weights(global_model)
                    for edge_agg_obj in edge_aggregators.values():
                        edge_agg_obj.global_model.load_state_dict(
                            copy.deepcopy(global_model.state_dict())
                        )

                    central_agg_this_round = central_agg

                # ── ② 推論 + AP ネットワーク条件シミュレーション ───────
                # 実機 measureAndLogTerminalSatisfaction (= observe) を各サイクル後に呼ぶ
                # のと等価。1G あたり 5 回判定。
                rng_net = np.random.default_rng(self.seed + composite_id * 100)

                # 現在の接続台数を集計（端末 _current_ap から）
                n_on_ap = [0] * self.num_aps
                for term in all_terminals:
                    if term._current_ap < self.num_aps:
                        n_on_ap[term._current_ap] += 1
                    else:
                        n_on_ap[0] += 1  # fallback

                # 背景負荷(時間変動): 混雑計算では端末台数 + 背景負荷を実効台数とする。
                bg_on_ap = self._background_on_ap(composite_id)

                # 各APのTP/RTT を決定（サイクルごとに再計算）
                if self.use_queue_model:
                    tp_rtt_per_ap: list[tuple[float, float]] = []
                    for ap_idx in range(self.num_aps):
                        p = self.ap_queue_params[ap_idx]
                        tp_val, rtt_val = ap_conditions(
                            n_connected   = n_on_ap[ap_idx] + bg_on_ap[ap_idx],
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
                self._write_congestion_log(composite_id, n_on_ap, tp_rtt_per_ap, c_summary)

                # 中央協調 (CTDE フェーズ1): 全体HM最大の joint 割当を計算。
                # 候補割当ごとに接続台数が変わる混雑を反映するため、キュー模型で
                # 決定論的に (台数)->(tp,rtt) を予測する関数を渡す。
                forced_assign: list[int] | None = None
                if self.decision_mode == "coordinated" and self.use_queue_model:
                    def _congestion_fn(n: int, ap: int, _bg=bg_on_ap) -> tuple[float, float]:
                        p = self.ap_queue_params[ap]
                        return ap_conditions(
                            n_connected  = n + _bg[ap],
                            mu_rtt       = p["mu_rtt"],
                            base_rtt_ms  = p["base_rtt_ms"],
                            n_channels   = p["n_channels"],
                            tp_init_mbps = p["tp_init_mbps"],
                            rng          = rng_net,
                            rtt_noise_std= 0.0,   # 協調のランキングは無ノイズ(決定論)
                            tp_noise_std = 0.0,
                        )
                    forced_assign = central_coordinator(
                        self._terminal_app_idx, self.num_aps, _congestion_fn
                    )

                # 各端末の推論 (round_id = composite_id でクールダウン判定もサイクル単位)
                inf_results: list[InferenceResult] = []
                for tidx, term in enumerate(all_terminals):
                    app = self._terminal_app_idx[tidx]
                    satisfactions = [terminal_satisfaction(app, tp, rtt) for tp, rtt in tp_rtt_per_ap]
                    best_ap = satisfactions.index(max(satisfactions)) if satisfactions else 0
                    inf = term.run_inference(
                        run_id   = self.run_id,
                        round_id = composite_id,
                        ap_conditions = tp_rtt_per_ap,
                        app_idx  = app,
                        best_ap  = best_ap,
                        baseline_mode = self.baseline_mode,
                        rng           = rng_baseline,
                        forced_ap     = (forced_assign[tidx] if forced_assign is not None else None),
                    )
                    inf_results.append(inf)

                # 混雑の測り直し(settle_congestion): 全端末が AP を選んだ"後"の
                # 実接続台数で tp/rtt を再計算し、満足度を測り直す。従来は意思決定"前"の
                # 台数で測るため、移動する政策(greedy/independent)は自らの混雑寄与が
                # 過小計上され満足度が水増しされる。これを是正して公平に比較する。
                if self.settle_congestion and self.use_queue_model:
                    n_on_ap_post = [0] * self.num_aps
                    for term in all_terminals:
                        ap = term._current_ap if term._current_ap < self.num_aps else 0
                        n_on_ap_post[ap] += 1
                    tp_rtt_post: list[tuple[float, float]] = []
                    for ap_idx in range(self.num_aps):
                        p = self.ap_queue_params[ap_idx]
                        tp_rtt_post.append(ap_conditions(
                            n_connected  = n_on_ap_post[ap_idx] + bg_on_ap[ap_idx],
                            mu_rtt       = p["mu_rtt"],
                            base_rtt_ms  = p["base_rtt_ms"],
                            n_channels   = p["n_channels"],
                            tp_init_mbps = p["tp_init_mbps"],
                            rng          = rng_net,
                            rtt_noise_std= p["rtt_noise_std"],
                            tp_noise_std = p["tp_noise_std"],
                        ))
                    for tidx, (term, inf) in enumerate(zip(all_terminals, inf_results)):
                        ap = term._current_ap if term._current_ap < self.num_aps else 0
                        tp_s, rtt_s = tp_rtt_post[ap]
                        inf.satisfaction = round(
                            terminal_satisfaction(self._terminal_app_idx[tidx], tp_s, rtt_s), 6
                        )
                    n_on_ap = n_on_ap_post  # 集計ログにも意思決定後の台数を反映

                for inf in inf_results:
                    self._write_inference_log(inf)

                # 満足度集計 (サイクル単位)
                avg_satisfaction = (
                    sum(r.satisfaction for r in inf_results) / max(len(inf_results), 1)
                )
                self._write_satisfaction_log(composite_id, inf_results, avg_satisfaction, n_on_ap)

            # 5サイクル完了後の最終集約結果を agg_history に記録 (G 単位の集計用)
            agg_history.append(central_agg_this_round)

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
    # セントラル集約用の TrainingResult 生成
    # エッジモデルを「仮想端末」として扱い、既存の FedAvgAggregator を再利用する。
    # ------------------------------------------------------------------ #
    def _background_on_ap(self, composite_id: int) -> list[int]:
        """各AP に乗る背景負荷(外部ユーザ台数)を返す。エピソード的・非対称・決定論的。
        dwell サイクルごとに 1つの AP(または none)へ hi 台の背景負荷を割り当てる。
        """
        if not self.bg_load_cfg:
            return [0] * self.num_aps
        hi    = int(self.bg_load_cfg.get("hi", 3))
        dwell = max(1, int(self.bg_load_cfg.get("dwell", 5)))
        episode = composite_id // dwell
        r = np.random.default_rng(self.seed + 90000 + episode)
        who = int(r.integers(0, self.num_aps + 1))  # num_aps 番目 = どのAPにも乗せない
        bg = [0] * self.num_aps
        if who < self.num_aps:
            bg[who] = hi
        return bg

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
