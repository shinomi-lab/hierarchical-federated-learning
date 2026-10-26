"""
analysis/compare.py
--------------------
シミュレーション結果 vs 理論値 の比較レポートを生成する。

出力:
  - results/reports/<run_id>_comparison.jsonl  ─ ラウンドごとの比較ログ
  - results/reports/<run_id>_summary.json      ─ 全体サマリー
  - (オプション) results/reports/<run_id>_plot.png ─ 収束曲線グラフ
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from sim.aggregator import AggregationResult
from sim.model import APSelectionMLP
from theory.fedavg_bounds import FedAvgBounds, TheoreticalParams, estimate_params_from_history


class ComparisonReporter:
    """
    シミュレーション結果と理論収束上界を比較し、レポートを生成する。
    """

    def __init__(
        self,
        run_id:      str,
        results_dir: str | Path = "results",
        save_plots:  bool = True,
    ) -> None:
        self.run_id      = run_id
        self.results_dir = Path(results_dir)
        self.save_plots  = save_plots
        self.report_dir  = self.results_dir / "reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # メイン比較
    # ------------------------------------------------------------------ #
    def compare(
        self,
        agg_history:  list[AggregationResult],
        params:       Optional[TheoreticalParams] = None,
        eta:          float = 0.01,
        K:            int   = 5,
        E:            int   = 5,
        iid:          bool  = True,
        fraction_fit: float = 1.0,
        input_size:   int   = 4,
        hidden_size:  int   = 8,
        output_size:  int   = 2,
    ) -> dict:
        """
        シミュレーション結果と理論値を比較し、ラウンドごとのギャップを計算。

        Args:
            K: 各ラウンドの実際の参加端末数 (= num_terminals × fraction_fit)
        """
        if params is None:
            params = estimate_params_from_history(agg_history, eta=eta, K=K, E=E, iid=iid)

        bounds    = FedAvgBounds(params)
        theory    = bounds.bounds_over_rounds(len(agg_history))

        # [修正] APSelectionMLP.param_count() で3層+LayerNorm込みの正しい数を使用
        model_params = APSelectionMLP.param_count(input_size, hidden_size, output_size)
        comm_cost = FedAvgBounds.communication_cost(
            num_rounds    = len(agg_history),
            num_terminals = K,
            model_params  = model_params,
            fraction_fit  = fraction_fit,
        )

        comparison_rows: list[dict] = []
        for agg, th in zip(agg_history, theory):
            sim_loss = agg.avg_val_loss
            th_loss  = th["theory_loss"]
            gap      = sim_loss - th_loss

            row = {
                "schema_version":     1,
                "ts_ms":              int(time.time() * 1000),
                "event":              "round_comparison",
                "run_id":             self.run_id,
                "round_id":           agg.round_id,
                "sim_val_loss":       agg.avg_val_loss,
                "sim_val_acc":        agg.avg_val_acc,
                "sim_train_loss":     agg.avg_train_loss,
                "theory_loss_bound":  round(th_loss, 6),
                "gap_loss":           round(gap, 6),
                "is_iid":             iid,
                "num_participants":   agg.num_participants,
                "global_weight_norm": agg.global_weight_norm,
            }
            comparison_rows.append(row)

        # JSONL 書き出し
        cmp_path = self.report_dir / f"{self.run_id[:8]}_comparison.jsonl"
        with open(cmp_path, "w", encoding="utf-8") as f:
            for row in comparison_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        # サマリー
        final_sim_acc  = agg_history[-1].avg_val_acc if agg_history else 0.0
        final_sim_loss = agg_history[-1].avg_val_loss if agg_history else 0.0
        avg_gap        = sum(r["gap_loss"] for r in comparison_rows) / max(len(comparison_rows), 1)

        # 収束ラウンド推定 (val_acc が最終値の 95% を超えた最初のラウンド)
        target_acc = final_sim_acc * 0.95
        conv_round = next(
            (a.round_id for a in agg_history if a.avg_val_acc >= target_acc),
            len(agg_history),
        )

        summary = {
            "run_id":              self.run_id,
            "num_rounds":          len(agg_history),
            "num_terminals_used":  K,
            "local_epochs":        E,
            "learning_rate":       eta,
            "iid":                 iid,
            "final_sim_val_acc":   round(final_sim_acc,  6),
            "final_sim_val_loss":  round(final_sim_loss, 6),
            "avg_gap_loss":        round(avg_gap, 6),
            "convergence_round":   conv_round,
            "model_params":        model_params,
            "communication_cost":  comm_cost,
            "theory_params": {
                "L":       params.L,
                "mu":      params.mu,
                "sigma_sq": params.sigma_sq,
                "G_sq":    params.G_sq,
                "F0":      params.F0,
            },
            "note": (
                "theory_loss_bound は Li et al.2020 に基づく損失ギャップの上界。"
                "sim_val_loss が上界を下回れば理論と整合している。"
                "精度の理論値は提供しない (CE loss と accuracy の変換は非線形)。"
            ),
        }

        sum_path = self.report_dir / f"{self.run_id[:8]}_summary.json"
        with open(sum_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        try:
            from sim.display import print_done as _done
        except Exception:
            def _done(msg: str) -> None: print(f"  ✔ {msg}")  # type: ignore[misc]
        _done(f"比較ログ : {cmp_path}")
        _done(f"サマリー : {sum_path}")

        if self.save_plots:
            self._plot(agg_history, comparison_rows)

        return summary

    # ------------------------------------------------------------------ #
    # グラフ描画
    # ------------------------------------------------------------------ #
    def _plot(self, agg_history: list[AggregationResult], rows: list[dict]) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            try:
                from sim.display import print_warn as _warn
            except Exception:
                def _warn(msg: str) -> None: print(f"  ⚠ {msg}")  # type: ignore[misc]
            _warn("matplotlib が見つかりません。グラフをスキップします。")
            return

        rounds      = [a.round_id     for a in agg_history]
        sim_acc     = [a.avg_val_acc  for a in agg_history]
        sim_loss    = [a.avg_val_loss for a in agg_history]
        theory_loss = [r["theory_loss_bound"] for r in rows]

        fig, axes = plt.subplots(1, 2, figsize=(13, 5))
        fig.suptitle(f"FedAvg Simulation vs Theory  (run={self.run_id[:8]})", fontsize=13)

        # Accuracy (simulation only; no theoretical accuracy provided)
        ax = axes[0]
        ax.plot(rounds, sim_acc, label="Simulation val_acc", color="steelblue", linewidth=2)
        ax.set_xlabel("Communication Round")
        ax.set_ylabel("Validation Accuracy")
        ax.set_title("Accuracy (Simulation)")
        ax.legend()
        ax.grid(alpha=0.3)
        ax.set_ylim(0, 1.05)

        # Loss: simulation vs theoretical upper bound
        ax = axes[1]
        ax.plot(rounds, sim_loss,    label="Simulation val_loss",   color="steelblue", linewidth=2)
        ax.plot(rounds, theory_loss, label="Theory loss bound (UB)", color="tomato",
                linestyle="--", linewidth=1.5)
        ax.set_xlabel("Communication Round")
        ax.set_ylabel("Validation Loss")
        ax.set_title("Loss: Simulation vs Theory Bound")
        ax.legend()
        ax.grid(alpha=0.3)

        plt.tight_layout()
        plot_path = self.report_dir / f"{self.run_id[:8]}_plot.png"
        plt.savefig(plot_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        try:
            from sim.display import print_done as _done2
        except Exception:
            def _done2(msg: str) -> None: print(f"  ✔ {msg}")  # type: ignore[misc]
        _done2(f"グラフ   : {plot_path}")
