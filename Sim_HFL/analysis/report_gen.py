"""
analysis/report_gen.py
-----------------------
シミュレーション実験ごとに Markdown レポートを生成する。

実機側 central_server/ai_advisor.py および tools/collect_events.py の
ログ収集・レポート構造を参考に、Sim_HFL の JSONL ログに合わせて設計。

出力先:
  results/reports/<run_id[:8]>_report_<YYYYMMDD_HHMMSS>.md

レポート構成:
  # 実験レポート
  ## 実験概要        ← 設定パラメータ
  ## 収束サマリー    ← 最終精度・収束ラウンド・通信コスト
  ## ラウンド別推移  ← ASCII テーブル（全ラウンド）
  ## 端末別統計      ← 端末ごとの平均 val_acc / train 時間
  ## 異常検知        ← NaN除外・極端な精度低下
  ## 理論比較        ← --compare 時のみ
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ------------------------------------------------------------------ #
# JSONL ユーティリティ（実機 collect_events.py の read_jsonl と同構造）
# ------------------------------------------------------------------ #
def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _filter(rows: list[dict], event: str) -> list[dict]:
    return [r for r in rows if r.get("event") == event]


def _dist_label(dat: dict) -> str:
    if dat.get("iid", True):
        return "IID"
    alpha = dat.get("dirichlet_alpha", 0.5)
    return f"non-IID (alpha={alpha})"


# ------------------------------------------------------------------ #
# メインクラス
# ------------------------------------------------------------------ #
class ReportGenerator:
    """
    JSONL ログから Markdown レポートを生成する。
    実機側の ai_advisor.generate_experiment_summary() に相当するが、
    LLM なしで決定論的なレポートを生成する。
    """

    def __init__(
        self,
        run_id:      str,
        log_path:    Path,
        results_dir: Path,
        cfg:         dict,
        comparison_jsonl: Optional[Path] = None,
    ) -> None:
        self.run_id           = run_id
        self.log_path         = log_path
        self.results_dir      = results_dir
        self.cfg              = cfg
        self.comparison_jsonl = comparison_jsonl

        self.report_dir = results_dir / "reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 公開 API
    # ------------------------------------------------------------------ #
    def generate(self) -> Path:
        """レポートを生成し、保存したパスを返す。"""
        rows     = _read_jsonl(self.log_path)
        agg_rows = _filter(rows, "aggregation_complete")
        trm_rows = _filter(rows, "terminal_round_complete")
        cmp_rows = _read_jsonl(self.comparison_jsonl) if self.comparison_jsonl else []
        cmp_rows = [r for r in cmp_rows if r.get("event") == "round_comparison"]

        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.report_dir / f"{self.run_id[:8]}_report_{ts}.md"

        inf_rows = _filter(rows, "inference_result")
        sat_rows = _filter(rows, "satisfaction_summary")

        sections = [
            self._header(ts),
            self._section_config(),
            self._section_convergence(agg_rows),
            self._section_rounds_table(agg_rows, sat_rows, inf_rows),
            self._section_ap_selection(inf_rows, sat_rows),
            self._section_terminals(trm_rows),
            self._section_anomalies(agg_rows, trm_rows),
        ]
        if cmp_rows:
            sections.append(self._section_theory(cmp_rows, agg_rows))

        md = "\n\n".join(sections) + "\n"

        out_path.write_text(md, encoding="utf-8")
        return out_path

    # ------------------------------------------------------------------ #
    # セクション生成
    # ------------------------------------------------------------------ #
    def _header(self, ts: str) -> str:
        iid_label = "IID" if self.cfg.get("data", {}).get("iid", True) else "non-IID"
        return (
            f"# Sim_HFL 実験レポート\n\n"
            f"- **run_id** : `{self.run_id}`\n"
            f"- **生成日時**: {ts[:4]}-{ts[4:6]}-{ts[6:8]} "
            f"{ts[9:11]}:{ts[11:13]}:{ts[13:15]}\n"
            f"- **ログ**   : `{self.log_path.name}`\n"
            f"- **データ分布**: {iid_label}"
        )

    # ---- 実験設定 ----
    def _section_config(self) -> str:
        fed = self.cfg.get("federation", {})
        lt  = self.cfg.get("local_training", {})
        dat = self.cfg.get("data", {})
        mdl = self.cfg.get("model", {})

        lines = [
            "## 実験設定\n",
            "| パラメータ | 値 |",
            "|---|---|",
            f"| 端末数 (K) | {fed.get('num_terminals', '?')} |",
            f"| 通信ラウンド数 (T) | {fed.get('num_rounds', '?')} |",
            f"| 参加割合 (fraction_fit) | {fed.get('fraction_fit', 1.0)} |",
            f"| ローカルエポック数 (E) | {lt.get('epochs', '?')} |",
            f"| バッチサイズ | {lt.get('batch_size', '?')} |",
            f"| 学習率 | {lt.get('learning_rate', '?')} |",
            f"| Weight Decay | {lt.get('weight_decay', '?')} |",
            f"| 勾配クリッピング | {lt.get('clip_max_norm', '?')} |",
            f"| Warmup Steps | {lt.get('warmup_steps', 0)} |",
            f"| Min LR Ratio | {lt.get('min_lr_ratio', 0.0)} |",
            f"| Dropout | {mdl.get('dropout_p', 0.0)} |",
            f"| データ分布 | {_dist_label(dat)} |",
            f"| 総サンプル数 | {dat.get('total_samples', '?')} |",
            f"| モデル構造 | {mdl.get('input_size',4)}→{mdl.get('hidden_size',8)}→{mdl.get('hidden_size',8)}→{mdl.get('output_size',2)} |",
        ]
        return "\n".join(lines)

    @staticmethod
    def _last_cycle_rows(agg_rows: list[dict]) -> list[dict]:
        """
        aggregation_complete は local_round=1〜5 × global_round=1〜30 で計150件。
        各グローバルラウンドの最終サイクル (local_round 最大) を1件ずつ抽出する。
        local_round フィールドがない旧フォーマットは全件をそのまま返す。
        """
        has_local = any("local_round" in r for r in agg_rows)
        if not has_local:
            return agg_rows
        by_g: dict[int, dict] = {}
        for r in agg_rows:
            g = r.get("global_round") or r.get("round_id", 0)
            l = r.get("local_round", 0)
            if g not in by_g or l > by_g[g].get("local_round", 0):
                by_g[g] = r
        return [by_g[g] for g in sorted(by_g)]

    # ---- 収束サマリー ----
    def _section_convergence(self, agg_rows: list[dict]) -> str:
        if not agg_rows:
            return "## 収束サマリー\n\nデータなし"

        # 各グローバルラウンドの最終サイクル (local_round=5) を代表値に使う
        rep_rows   = self._last_cycle_rows(agg_rows)
        final      = rep_rows[-1]
        final_acc  = final.get("avg_val_acc", 0.0)
        final_loss = final.get("avg_val_loss", 0.0)
        num_grnd   = len(rep_rows)

        target    = final_acc * 0.95
        conv_grnd = next(
            (r.get("global_round") or r.get("round_id", num_grnd)
             for r in rep_rows if r.get("avg_val_acc", 0) >= target),
            num_grnd,
        )

        fed     = self.cfg.get("federation", {})
        mdl     = self.cfg.get("model", {})
        lt      = self.cfg.get("local_training", {})
        K       = max(1, int(fed.get("num_terminals", 5) * fed.get("fraction_fit", 1.0)))
        h       = mdl.get("hidden_size", 8)
        inp     = mdl.get("input_size", 4)
        out     = mdl.get("output_size", 2)
        local_r = fed.get("local_rounds", 5)
        params  = inp*h+h + h+h + h*h+h + h+h + h*out+out
        # 全集約回数 = グローバルラウンド × ローカルサイクル数
        total_agg   = num_grnd * local_r
        total_bytes = params * 4 * 2 * K * total_agg
        total_mb    = total_bytes / 1e6

        all_accs = [r.get("avg_val_acc", 0) for r in rep_rows]
        mean_acc = statistics.mean(all_accs) if all_accs else 0.0
        max_acc  = max(all_accs) if all_accs else 0.0
        max_grnd = all_accs.index(max_acc) + 1

        excl_rnds = [
            r.get("global_round") or r.get("round_id")
            for r in rep_rows if r.get("num_excluded", 0) > 0
        ]

        lines = [
            "## 収束サマリー\n",
            f"> 1グローバルラウンド = {local_r}ローカルサイクル × "
            f"{lt.get('epochs', 5)}エポック = "
            f"{local_r * lt.get('epochs', 5)}エポック/セッション\n",
            "| 指標 | 値 |",
            "|---|---|",
            f"| 最終 val_acc | **{final_acc:.4f}** ({final_acc*100:.2f}%) |",
            f"| 最終 val_loss | {final_loss:.4f} |",
            f"| 最大 val_acc | {max_acc:.4f} (Global Round {max_grnd}) |",
            f"| 全ラウンド平均 val_acc | {mean_acc:.4f} |",
            f"| 収束ラウンド推定 (≥95%最終精度) | Global Round {conv_grnd} / {num_grnd} |",
            f"| 総通信量 | {total_mb:.3f} MB ({total_bytes:,} bytes) |",
            f"| モデルパラメータ数 | {params:,} |",
            f"| NaN/Inf 除外発生ラウンド | "
            f"{', '.join(f'G{r}' for r in excl_rnds) if excl_rnds else 'なし'} |",
        ]
        return "\n".join(lines)

    # ---- ラウンド別テーブル ----
    def _section_rounds_table(
        self, agg_rows: list[dict], sat_rows: list[dict], inf_rows: list[dict] | None = None
    ) -> str:
        if not agg_rows:
            return "## ラウンド別推移\n\nデータなし"

        # 各グローバルラウンドの最終サイクルを代表値として使う
        rep_rows = self._last_cycle_rows(agg_rows)

        # 満足度・切替数 (global_round でキー)
        sat_before_map: dict[int, float] = {}
        sat_after_map:  dict[int, float] = {}
        switch_by_grnd: dict[int, int]   = {}
        for r in sat_rows:
            g = r.get("round_id", -1)   # satisfaction_summary は global_round でログ
            switch_by_grnd[g] = r.get("n_switched", 0)

        # inf_rows から before/after を global_round 単位で集計
        inf_bef_by_g: dict[int, list[float]] = {}
        inf_aft_by_g: dict[int, list[float]] = {}
        for r in (inf_rows or []):
            g = r.get("round_id", -1)   # inference_result も global_round でログ
            inf_bef_by_g.setdefault(g, []).append(
                r.get("satisfaction_before", r.get("satisfaction", 0.0))
            )
            inf_aft_by_g.setdefault(g, []).append(r.get("satisfaction", 0.0))
        for g, vals in inf_bef_by_g.items():
            sat_before_map[g] = sum(vals) / max(len(vals), 1)
        for g, vals in inf_aft_by_g.items():
            sat_after_map[g] = sum(vals) / max(len(vals), 1)

        lines = [
            "## ラウンド別推移\n",
            "> 各行 = グローバルラウンド (1セッション = 5サイクル × 5エポック = 25エポック)  \n"
            "> sat_before = 切替前AP満足度 / sat_after = 切替後AP満足度（実機 satisfaction_after 相当）\n",
            "| G.Round | val_acc | val_loss | train_loss | sat_before | sat_after | AP切替 | participants |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in rep_rows:
            g_rnd    = r.get("global_round") or r.get("round_id", "?")
            acc      = r.get("avg_val_acc", 0.0)
            vloss    = r.get("avg_val_loss", 0.0)
            tloss    = r.get("avg_train_loss", 0.0)
            part     = r.get("num_participants", "?")
            sat_bef  = sat_before_map.get(g_rnd, None)
            sat_aft  = sat_after_map.get(g_rnd, None)
            n_sw     = switch_by_grnd.get(g_rnd, 0)
            bar      = "█" * int(acc * 10) + "░" * (10 - int(acc * 10))
            bef_str  = f"{sat_bef*100:.1f}%" if sat_bef is not None else "-"
            aft_str  = f"{sat_aft*100:.1f}%" if sat_aft is not None else "-"
            sw_str   = f"**{n_sw}**" if n_sw > 0 else "-"
            lines.append(
                f"| {g_rnd:>3} | {acc:.4f} `{bar}` | {vloss:.4f} | {tloss:.4f}"
                f" | {bef_str} | {aft_str} | {sw_str} | {part} |"
            )

        return "\n".join(lines)

    # ---- AP選択・満足度サマリー ----
    def _section_ap_selection(
        self, inf_rows: list[dict], sat_rows: list[dict]
    ) -> str:
        if not inf_rows:
            return "## AP選択・満足度\n\nデータなし"

        # ── 端末別集計 ──────────────────────────────────────────
        by_terminal: dict[str, list[dict]] = {}
        for r in inf_rows:
            tid = r.get("terminal_id", "?")
            by_terminal.setdefault(tid, []).append(r)

        term_lines = [
            "## AP選択・満足度\n",
            "### 端末別 AP選択サマリー\n",
            "> satisfaction_before = 切替前APの満足度（切替判断の根拠）  \n"
            "> satisfaction_after  = 切替後APの満足度（実際に得られた品質）\n",
            "| 端末 | 最終AP | 切替回数 | sat_before (avg) | sat_after (avg) | 平均予測ティア | 確信度 |",
            "|---|---|---|---|---|---|---|",
        ]
        for tid in sorted(by_terminal):
            recs       = by_terminal[tid]
            final_ap   = f"AP{'A' if recs[-1].get('predicted_ap', 0) == 0 else 'B'}"
            n_switch   = sum(1 for r in recs if r.get("switched", False))
            avg_before = sum(r.get("satisfaction_before", r.get("satisfaction", 0)) for r in recs) / max(len(recs), 1)
            avg_after  = sum(r.get("satisfaction", 0) for r in recs) / max(len(recs), 1)
            avg_tier   = sum(r.get("predicted_tier", 0) for r in recs) / max(len(recs), 1)
            avg_conf   = sum(r.get("ap_confidence", r.get("avg_confidence", 0)) for r in recs) / max(len(recs), 1)
            delta      = avg_after - avg_before
            delta_str  = f"({delta:+.3f})"
            term_lines.append(
                f"| `{tid}` | {final_ap} | {n_switch} "
                f"| {avg_before:.4f} ({avg_before*100:.1f}%) "
                f"| {avg_after:.4f} ({avg_after*100:.1f}%) {delta_str} "
                f"| {avg_tier:.2f} | {avg_conf:.4f} |"
            )

        # ── 満足度ティア分布 ──────────────────────────────────
        tier_counts = [0, 0, 0, 0]
        for r in inf_rows:
            t = r.get("predicted_tier", 0)
            if 0 <= t < 4:
                tier_counts[t] += 1
        total_inf = max(sum(tier_counts), 1)

        tier_labels = ["tier0 very_low (<25%)", "tier1 low (25-50%)", "tier2 medium (50-75%)", "tier3 high (≥75%)"]
        tier_lines = [
            "\n### 予測満足度ティア分布（全端末・全ラウンド）\n",
            "| ティア | 意味 | 件数 | 割合 |",
            "|---|---|---|---|",
        ]
        for i, (label, cnt) in enumerate(zip(tier_labels, tier_counts)):
            bar = "█" * int(cnt / total_inf * 20) + "░" * (20 - int(cnt / total_inf * 20))
            tier_lines.append(
                f"| tier {i} | {label.split(' ', 1)[1]} | {cnt} | {cnt/total_inf*100:.1f}% `{bar}` |"
            )

        # ── ラウンド別満足度推移 ──────────────────────────────
        # sat_rows から before/after を再集計 (inf_rows ベース)
        sat_before_by_round: dict[int, list[float]] = {}
        sat_after_by_round:  dict[int, list[float]] = {}
        for r in inf_rows:
            rnd = r.get("round_id", -1)
            sat_before_by_round.setdefault(rnd, []).append(
                r.get("satisfaction_before", r.get("satisfaction", 0.0))
            )
            sat_after_by_round.setdefault(rnd, []).append(r.get("satisfaction", 0.0))

        sat_lines = ["\n### ラウンド別満足度統計\n",
                     "> sat_before = 切替前AP / sat_after = 切替後AP（実機 satisfaction_after 相当）  \n"
                     "> harm_mean = 調和平均（最不満端末を重視）/ min = 最低満足度 / std = 標準偏差 / overflow = 容量超過端末数\n"]
        if sat_rows:
            sat_lines += [
                "| Round | sat_before | sat_after (avg) | HM_before | HM_after | min | std | AP切替 | overflow |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            # 移動平均用バッファ (直近3ラウンド)
            _ma_window: list[float] = []
            for r in sorted(sat_rows, key=lambda x: x.get("round_id", 0)):
                rnd    = r.get("round_id", "?")
                n_sw   = r.get("n_switched", 0)
                sw_str = f"**{n_sw}**" if n_sw > 0 else "-"
                bef_list = sat_before_by_round.get(rnd, [])
                aft_list = sat_after_by_round.get(rnd, [])
                bef  = sum(bef_list) / max(len(bef_list), 1)
                aft  = sum(aft_list) / max(len(aft_list), 1)
                hm   = r.get("harmonic_mean_satisfaction", None)
                hm_b = r.get("harmonic_mean_satisfaction_before", None)
                mns  = r.get("min_satisfaction", None)
                std  = r.get("satisfaction_std", None)
                ovf  = r.get("capacity_overflow_count", "-")
                hm_str  = f"{hm*100:.1f}%" if hm is not None else "-"
                hm_b_str = f"{hm_b*100:.1f}%" if hm_b is not None else "-"
                min_str = f"{mns*100:.1f}%" if mns is not None else "-"
                std_str = f"{std:.3f}"    if std is not None else "-"
                _ma_window.append(aft)
                if len(_ma_window) > 3:
                    _ma_window.pop(0)
                ma = sum(_ma_window) / len(_ma_window)
                bar = "█" * int(ma * 10) + "░" * (10 - int(ma * 10))
                sat_lines.append(
                    f"| {rnd:>3} | {bef*100:.1f}% | {aft*100:.1f}% `{bar}` (MA={ma*100:.1f}%)"
                    f" | {hm_b_str} | {hm_str} | {min_str} | {std_str} | {sw_str} | {ovf} |"
                )
        else:
            sat_lines.append("> 満足度ログなし")

        # ── AP接続台数推移 ──────────────────────────────────
        ap_load_lines = ["\n### APごとの接続台数推移\n"]
        ap_load_data: dict[int, list[tuple[int, int]]] = {}   # ap_idx → [(round, n)]
        for r in sorted(sat_rows, key=lambda x: x.get("round_id", 0)):
            rnd    = r.get("round_id", -1)
            n_on_ap = r.get("n_on_ap") or []
            for ap_idx, n in enumerate(n_on_ap):
                ap_load_data.setdefault(ap_idx, []).append((rnd, n))
        if ap_load_data:
            ap_ids = sorted(ap_load_data.keys())
            header = "| Round | " + " | ".join(f"AP{i}" for i in ap_ids) + " |"
            sep    = "|---|" + "---|" * len(ap_ids)
            ap_load_lines += [header, sep]
            rounds_seen: dict[int, dict[int, int]] = {}
            for ap_idx, entries in ap_load_data.items():
                for rnd, n in entries:
                    rounds_seen.setdefault(rnd, {})[ap_idx] = n
            for rnd in sorted(rounds_seen):
                row_vals = rounds_seen[rnd]
                row = f"| {rnd:>3} | " + " | ".join(str(row_vals.get(i, "-")) for i in ap_ids) + " |"
                ap_load_lines.append(row)
        else:
            ap_load_lines.append("> n_on_ap データなし")

        # ── 個別満足度分布テーブル ──────────────────────────
        dist_lines = ["\n### 個別端末満足度分布（全ラウンド集計）\n",
                      "> 各端末の satisfaction_before / satisfaction_after の統計量\n"]
        by_term_sat_before: dict[str, list[float]] = {}
        by_term_sat_after:  dict[str, list[float]] = {}
        for r in inf_rows:
            tid = r.get("terminal_id", "?")
            by_term_sat_before.setdefault(tid, []).append(
                r.get("satisfaction_before", r.get("satisfaction", 0.0))
            )
            by_term_sat_after.setdefault(tid, []).append(r.get("satisfaction", 0.0))
        if by_term_sat_after:
            dist_lines += [
                "| 端末 | 件数 | before(avg) | before(HM) | after(avg) | after(HM) | delta | after(min) | after(max) | after(std) |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
            _eps = 1e-9
            for tid in sorted(by_term_sat_after):
                vals_a = by_term_sat_after[tid]
                vals_b = by_term_sat_before.get(tid, vals_a)
                avg_a = sum(vals_a) / len(vals_a)
                avg_b = sum(vals_b) / len(vals_b)
                try:
                    hm_a = len(vals_a) / sum(1.0 / max(s, _eps) for s in vals_a)
                except Exception:
                    hm_a = 0.0
                try:
                    hm_b = len(vals_b) / sum(1.0 / max(s, _eps) for s in vals_b)
                except Exception:
                    hm_b = 0.0
                delta = avg_a - avg_b
                min_a = min(vals_a)
                max_a = max(vals_a)
                try:
                    std_a = statistics.stdev(vals_a) if len(vals_a) >= 2 else 0.0
                except Exception:
                    std_a = 0.0
                dist_lines.append(
                    f"| `{tid}` | {len(vals_a)} "
                    f"| {avg_b*100:.1f}% | {hm_b*100:.1f}% "
                    f"| {avg_a*100:.1f}% | {hm_a*100:.1f}% "
                    f"| {delta:+.3f} "
                    f"| {min_a*100:.1f}% | {max_a*100:.1f}% | {std_a:.3f} |"
                )

        # ── AP選択の解釈 ─────────────────────────────────────
        all_sats = [r.get("satisfaction", 0) for r in inf_rows]
        overall_sat = sum(all_sats) / max(len(all_sats), 1)
        tier3_ratio = tier_counts[3] / total_inf

        interp_lines = ["\n### 解釈\n"]
        if overall_sat >= 0.80:
            interp_lines.append(
                f"> **高満足度**: 全端末の平均満足度 {overall_sat:.1%}。"
                "端末が適切な AP に接続できており、モデルが正しく機能している。"
            )
        elif overall_sat >= 0.50:
            interp_lines.append(
                f"> **中程度の満足度**: 全端末の平均満足度 {overall_sat:.1%}。"
                "一部の端末が最適でない AP に接続している可能性がある。"
            )
        else:
            interp_lines.append(
                f"> **低満足度**: 全端末の平均満足度 {overall_sat:.1%}。"
                "AP選択モデルの学習が不十分か、AP条件の設定を見直すことを推奨。"
            )

        if tier3_ratio >= 0.60:
            interp_lines.append(
                f"\n> **ティア3 優勢** ({tier3_ratio:.1%}): "
                "モデルが高満足度状態を多く予測している。接続APが要件を概ね満たしている。"
            )
        elif tier3_ratio + tier_counts[2] / total_inf >= 0.60:
            interp_lines.append(
                f"\n> **ティア2・3 優勢**: "
                "モデルは概ね「満足」と判断しており、AP維持判断が多い。"
            )
        else:
            interp_lines.append(
                f"\n> **低ティア優勢**: ティア0・1が多い。"
                "AP切替が多発しているか、モデルが未収束の可能性がある。"
            )

        return "\n".join(term_lines + tier_lines + sat_lines + ap_load_lines + dist_lines + interp_lines)

    # ---- 端末別統計 ----
    def _section_terminals(self, trm_rows: list[dict]) -> str:
        if not trm_rows:
            return "## 端末別統計\n\nデータなし"

        # 端末IDごとに集計
        terminals: dict[str, list[dict]] = {}
        for r in trm_rows:
            tid = r.get("terminal_id", "unknown")
            terminals.setdefault(tid, []).append(r)

        lines = [
            "## 端末別統計\n",
            "| 端末 ID | 参加ラウンド | 平均 val_acc | 平均 val_loss | 平均学習時間(ms) | サンプル数 |",
            "|---|---|---|---|---|---|",
        ]
        for tid in sorted(terminals):
            recs    = terminals[tid]
            n_round = len(recs)
            avg_acc = statistics.mean(r.get("val_acc",  0.0) for r in recs)
            avg_vls = statistics.mean(r.get("val_loss", 0.0) for r in recs)
            avg_dur = statistics.mean(r.get("train_duration_ms", 0.0) for r in recs)
            n_samp  = recs[0].get("n_samples", "?")
            lines.append(
                f"| `{tid}` | {n_round} | {avg_acc:.4f} | {avg_vls:.4f} | {avg_dur:.1f} | {n_samp} |"
            )

        # 端末間分散（IID/non-IID の差の指標）
        if len(terminals) >= 2:
            final_accs = []
            for tid, recs in terminals.items():
                last = sorted(recs, key=lambda r: r.get("round_id", 0))[-1]
                final_accs.append(last.get("val_acc", 0.0))
            if len(final_accs) >= 2:
                stdev = statistics.stdev(final_accs)
                lines.append(f"\n> **端末間 val_acc 標準偏差（最終ラウンド）**: {stdev:.4f}  ")
                lines.append(
                    "> （値が小さいほど端末間の偏りが少なく、IID に近い学習ができている）"
                )

        return "\n".join(lines)

    # ---- 異常検知 ----
    def _section_anomalies(
        self, agg_rows: list[dict], trm_rows: list[dict]
    ) -> str:
        alerts: list[str] = []

        # 各グローバルラウンドの最終サイクルのみ使う
        rep_rows = self._last_cycle_rows(agg_rows)

        # 1. NaN/Inf 除外が発生したラウンド
        for r in rep_rows:
            excl = r.get("num_excluded", 0)
            if excl > 0:
                g = r.get("global_round") or r.get("round_id")
                alerts.append(
                    f"- **G.Round {g}**: {excl} 端末の更新に NaN/Inf を検出し除外した"
                )

        # 2. 精度の急落（前グローバルラウンド比 -5pt 以上）
        for i in range(1, len(rep_rows)):
            prev = rep_rows[i - 1].get("avg_val_acc", 0)
            curr = rep_rows[i].get("avg_val_acc", 0)
            if prev - curr > 0.05:
                g = rep_rows[i].get("global_round") or rep_rows[i].get("round_id")
                alerts.append(
                    f"- **G.Round {g}**: "
                    f"val_acc が急落 ({prev:.4f} → {curr:.4f}, Δ={curr-prev:+.4f})"
                )

        # 3. val_acc が最終ラウンドも低い（<60%）
        if rep_rows:
            final_acc = rep_rows[-1].get("avg_val_acc", 1.0)
            if final_acc < 0.60:
                alerts.append(
                    f"- **精度不足**: 最終 val_acc = {final_acc:.4f} (<60%) — "
                    "学習率・エポック数・データ量を見直すことを推奨"
                )

        # 4. 端末ごとの weight_norm が極端に大きい
        for r in trm_rows:
            wn = r.get("weight_norm", 0.0)
            if wn > 50.0:
                alerts.append(
                    f"- **Round {r.get('round_id')} / {r.get('terminal_id')}**: "
                    f"weight_norm が大きい ({wn:.2f}) — 勾配爆発の可能性"
                )

        section = "## 異常検知\n"
        if alerts:
            section += "\n".join(alerts)
        else:
            section += "> 異常なし — 全ラウンドで NaN/Inf 除外・精度急落・weight_norm 異常は検出されなかった"

        return section

    # ---- 理論比較 ----
    def _section_theory(
        self, cmp_rows: list[dict], agg_rows: list[dict]
    ) -> str:
        if not cmp_rows:
            return ""

        # sim loss が理論上界を下回っているか（「整合」の指標）
        consistent = sum(
            1 for r in cmp_rows
            if r.get("sim_val_loss", 999) <= r.get("theory_loss_bound", 0)
        )
        total       = len(cmp_rows)
        avg_gap     = statistics.mean(r.get("gap_loss", 0) for r in cmp_rows)
        final_cmp   = cmp_rows[-1]
        is_iid      = final_cmp.get("is_iid", True)

        # 収束判定：最後の 10% ラウンドで loss の変化が 1% 未満
        tail = cmp_rows[max(0, len(cmp_rows) - max(1, len(cmp_rows)//10)):]
        tail_losses  = [r.get("sim_val_loss", 0) for r in tail]
        loss_range   = max(tail_losses) - min(tail_losses) if tail_losses else 0
        converged    = loss_range < 0.01

        lines = [
            "## 理論値比較（Li et al. 2020 FedAvg 収束上界）\n",
            "| 指標 | 値 |",
            "|---|---|",
            f"| データ分布 | {'IID' if is_iid else 'non-IID'} |",
            f"| 理論上界との整合ラウンド数 | {consistent} / {total} "
            f"({'%.0f' % (consistent/total*100)}%) |",
            f"| 平均損失ギャップ (sim − bound) | {avg_gap:+.4f} |",
            f"| 収束判定（末尾 loss 変化 <1%） | {'✅ 収束済み' if converged else '⚠ 未収束'} |",
            "",
            "### 解釈\n",
        ]

        # 自動解釈
        if consistent / max(total, 1) >= 0.8:
            lines.append(
                "> **理論整合**: 80% 以上のラウンドで sim_val_loss が理論上界以下。"
                "FedAvg の収束保証が実験上も確認された。"
            )
        elif avg_gap < 0:
            lines.append(
                "> **理論より良好**: 平均損失ギャップが負（sim が上界を下回っている）。"
                "理論は悲観的な上界であり、実際の収束はより速い。"
            )
        else:
            lines.append(
                "> **理論上界超過**: 平均損失ギャップが正。"
                "理論パラメータ（L・μ・σ²）の推定精度か、"
                "データの非凸性の影響を検討する余地がある。"
            )

        if not is_iid:
            lines.append(
                "\n> **non-IID 注意**: 異質性バイアス Γ が存在するため、"
                "IID と比較して損失の残留ギャップが大きくなることは理論的に自然。"
            )

        # ラウンド別サマリーテーブル（抜粋: 最初・中間・最後）
        if total >= 3:
            idxs = [0, total // 2, total - 1]
            lines += [
                "\n### ラウンド抜粋\n",
                "| Round | sim val_loss | theory bound | gap |",
                "|---|---|---|---|",
            ]
            for i in idxs:
                r = cmp_rows[i]
                lines.append(
                    f"| {r['round_id']} | {r.get('sim_val_loss',0):.4f} "
                    f"| {r.get('theory_loss_bound',0):.4f} "
                    f"| {r.get('gap_loss',0):+.4f} |"
                )

        return "\n".join(lines)
