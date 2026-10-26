#!/usr/bin/env python3
"""
複数試行の横断分析スクリプト。

各試行(central_server_*.log)を analyze_latest_trial と同じロジックで解析し、
試行間を比較するグラフ・レポートを生成する。

使用例:
  # 直近5試行を分析
  python3 scripts/analyze_multi_trial.py -n 5

  # 直近10試行（短すぎるログは除外）
  python3 scripts/analyze_multi_trial.py -n 10 --min-lines 500

  # 特定の試行を指定
  python3 scripts/analyze_multi_trial.py --select \\
      logs/time_records/central_server_20260429_171040.log \\
      logs/time_records/central_server_20260429_143011.log

  # 全試行
  python3 scripts/analyze_multi_trial.py --all

  # インタラクティブ選択
  python3 scripts/analyze_multi_trial.py --interactive
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# analyze_latest_trial を再利用
sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_latest_trial as alt

ROOT = alt.ROOT


# ---------------------------------------------------------------------------
# Trial summary (per-trial aggregate)
# ---------------------------------------------------------------------------
@dataclass
class TrialSummary:
    trial_id: str
    central_path: Path
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    num_rounds: int = 0
    num_terminals: int = 0
    terminal_ids: List[str] = field(default_factory=list)
    # Per-terminal averages
    avg_accuracy: Optional[float] = None
    avg_loss: Optional[float] = None
    final_accuracy: Optional[float] = None
    final_loss: Optional[float] = None
    avg_satisfaction_before: Optional[float] = None
    avg_satisfaction_after: Optional[float] = None
    hm_satisfaction_before: Optional[float] = None
    hm_satisfaction_after: Optional[float] = None
    avg_training_ms: Optional[float] = None
    avg_upload_latency_ms: Optional[float] = None
    num_edge_agg: int = 0
    total_samples: int = 0
    # Raw DataFrames for cross-trial plots
    df: Any = None
    idx_df: Any = None
    rep: Any = None


def build_trial_summary(central_path: Path) -> Optional[TrialSummary]:
    """Run the analysis pipeline for one trial and extract summary stats."""
    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas required", file=sys.stderr)
        return None

    rep = alt.TrialReport(central_path=central_path)
    alt.parse_central(central_path, rep)
    window = (rep.window_start, rep.window_end)

    if rep.window_start is None:
        return None

    jsonl_files = alt.collect_round_and_edge_logs(window)
    for p in jsonl_files:
        alt.ingest_jsonl_file(p, rep, window)

    rep.manifests = alt.load_manifests_for_window(
        window, "received_files/terminal_updates/**/manifest_*.json"
    )
    rep.post_switch_telemetry = alt.load_post_switch_telemetry(window)
    rep.index_telemetry = alt.load_full_index_telemetry(window)
    alt._enrich_telemetry_from_index(rep)
    rep.upload_events = alt.load_upload_events(window)
    rep.edge_agg_meta = alt.load_edge_agg_meta(window)

    df = alt._telemetry_dataframe(rep)
    idx_df = alt._index_telemetry_dataframe(rep)

    # Build trial ID from log filename timestamp
    trial_id = central_path.stem.replace("central_server_", "")

    ts = TrialSummary(
        trial_id=trial_id,
        central_path=central_path,
        window_start=rep.window_start,
        window_end=rep.window_end,
        rep=rep,
        df=df,
        idx_df=idx_df,
    )

    if df is not None and not df.empty:
        # Data cleansing: detect and warn about anomalous data
        n_before = len(df)
        df = df.dropna(subset=["round_id"])
        n_dropped_rid = n_before - len(df)
        if n_dropped_rid > 0:
            print(f"\n    WARN: {n_dropped_rid} rows dropped (missing round_id)", end="", file=sys.stderr)

        # Detect zero-accuracy anomalies (possible incomplete trial)
        if "accuracy" in df.columns:
            zero_acc = (df["accuracy"] == 0).sum()
            nan_acc = df["accuracy"].isna().sum()
            total_acc = len(df)
            if zero_acc > total_acc * 0.5 and total_acc > 2:
                print(f"\n    WARN: {zero_acc}/{total_acc} rows have accuracy=0 (possibly incomplete trial)", end="", file=sys.stderr)
            if nan_acc > total_acc * 0.7 and total_acc > 2:
                print(f"\n    WARN: {nan_acc}/{total_acc} rows have accuracy=NaN", end="", file=sys.stderr)

        df_valid = df
        ts.terminal_ids = sorted(df_valid["terminal_id"].unique().tolist())
        ts.num_terminals = len(ts.terminal_ids)
        ts.num_rounds = int(df_valid["round_id"].nunique())

        if "accuracy" in df_valid.columns and df_valid["accuracy"].notna().any():
            ts.avg_accuracy = df_valid["accuracy"].mean()
            # Final round accuracy
            max_round = df_valid["round_id"].max()
            final = df_valid[df_valid["round_id"] == max_round]
            ts.final_accuracy = final["accuracy"].mean() if final["accuracy"].notna().any() else None

        if "loss" in df_valid.columns and df_valid["loss"].notna().any():
            ts.avg_loss = df_valid["loss"].mean()
            max_round = df_valid["round_id"].max()
            final = df_valid[df_valid["round_id"] == max_round]
            ts.final_loss = final["loss"].mean() if final["loss"].notna().any() else None

        if df_valid["satisfaction_before"].notna().any():
            ts.avg_satisfaction_before = df_valid["satisfaction_before"].mean()
            befs = df_valid["satisfaction_before"].dropna().tolist()
            ts.hm_satisfaction_before = alt._harmonic_mean(befs)

        if df_valid["satisfaction_after"].notna().any():
            ts.avg_satisfaction_after = df_valid["satisfaction_after"].mean()
            afts = df_valid["satisfaction_after"].dropna().tolist()
            ts.hm_satisfaction_after = alt._harmonic_mean(afts)

        if "total_local_ms" in df_valid.columns and df_valid["total_local_ms"].notna().any():
            ts.avg_training_ms = df_valid["total_local_ms"].mean()

    # Upload latency
    udf = alt._upload_timing_dataframe(rep)
    if udf is not None and not udf.empty:
        ts.avg_upload_latency_ms = udf["upload_duration_ms"].mean()

    # Edge aggregation
    ts.num_edge_agg = len(rep.edge_agg_meta)
    edf = alt._edge_agg_dataframe(rep)
    if edf is not None and not edf.empty and "sum_n_samples" in edf.columns:
        ts.total_samples = int(edf["sum_n_samples"].sum())

    return ts


# ---------------------------------------------------------------------------
# Cross-trial plots
# ---------------------------------------------------------------------------
def write_cross_trial_plots(summaries: List[TrialSummary], run_dir: Path) -> List[str]:
    plt = alt._setup_matplotlib()
    import numpy as np
    names = []

    n = len(summaries)
    labels = [s.trial_id for s in summaries]
    x = np.arange(n)

    # 1) Final Accuracy & Loss across trials
    accs = [s.final_accuracy for s in summaries]
    losses = [s.final_loss for s in summaries]
    if any(v is not None for v in accs):
        fig, ax1 = plt.subplots(figsize=(max(10, n * 1.2), 6))
        ax2 = ax1.twinx()
        acc_vals = [v if v is not None else float("nan") for v in accs]
        loss_vals = [v if v is not None else float("nan") for v in losses]
        bars1 = ax1.bar(x - 0.2, acc_vals, 0.35, label="Final Accuracy", color="steelblue", alpha=0.8)
        bars2 = ax2.bar(x + 0.2, loss_vals, 0.35, label="Final Loss", color="coral", alpha=0.8)
        ax1.set_xticks(x)
        ax1.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
        ax1.set_ylabel("Accuracy")
        ax2.set_ylabel("Loss")
        ax1.set_title("Final Round Accuracy & Loss Across Trials")
        ax1.legend(loc="upper left", fontsize=8)
        ax2.legend(loc="upper right", fontsize=8)
        ax1.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        p = run_dir / "cross_final_accuracy_loss.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    # 2) Accuracy learning curves with std error bands
    has_curves = any(s.df is not None and "accuracy" in s.df.columns and s.df["accuracy"].notna().any() for s in summaries)
    if has_curves:
        fig, ax = plt.subplots(figsize=(10, 6))
        cmap = plt.get_cmap("tab10")
        for i, s in enumerate(summaries):
            if s.df is None:
                continue
            df_v = s.df.dropna(subset=["round_id", "accuracy"])
            if df_v.empty:
                continue
            grouped = df_v.groupby("round_id")["accuracy"]
            means = grouped.mean().sort_index()
            stds = grouped.std().sort_index().fillna(0)
            c = cmap(i % 10)
            rx = means.index.astype(float)
            ax.plot(rx, means.values, marker="o", markersize=4,
                    color=c, label=s.trial_id, linewidth=2, alpha=0.8)
            ax.fill_between(rx, (means - stds).values, (means + stds).values,
                            color=c, alpha=0.15)
        ax.set_xlabel("Round ID")
        ax.set_ylabel("System Mean Accuracy")
        ax.set_title("Accuracy Learning Curves Across Trials (shading = 1 std)")
        ax.legend(fontsize=7, loc="lower right")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        p = run_dir / "cross_accuracy_curves.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    # 3) Loss learning curves with std error bands
    has_loss_curves = any(s.df is not None and "loss" in s.df.columns and s.df["loss"].notna().any() for s in summaries)
    if has_loss_curves:
        fig, ax = plt.subplots(figsize=(10, 6))
        cmap = plt.get_cmap("tab10")
        for i, s in enumerate(summaries):
            if s.df is None:
                continue
            df_v = s.df.dropna(subset=["round_id", "loss"])
            if df_v.empty:
                continue
            grouped = df_v.groupby("round_id")["loss"]
            means = grouped.mean().sort_index()
            stds = grouped.std().sort_index().fillna(0)
            c = cmap(i % 10)
            rx = means.index.astype(float)
            ax.plot(rx, means.values, marker="x", markersize=4,
                    linestyle="--", color=c, label=s.trial_id, linewidth=2, alpha=0.8)
            ax.fill_between(rx, (means - stds).values, (means + stds).values,
                            color=c, alpha=0.15)
        ax.set_xlabel("Round ID")
        ax.set_ylabel("System Mean Loss")
        ax.set_title("Loss Curves Across Trials (shading = 1 std)")
        ax.legend(fontsize=7, loc="upper right")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        p = run_dir / "cross_loss_curves.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    # 4) Satisfaction comparison (Before/After per trial)
    befs = [s.hm_satisfaction_before for s in summaries]
    afts = [s.hm_satisfaction_after for s in summaries]
    if any(v is not None and not math.isnan(v) for v in befs):
        fig, ax = plt.subplots(figsize=(max(10, n * 1.2), 6))
        bef_vals = [v if v is not None and not math.isnan(v) else 0 for v in befs]
        aft_vals = [v if v is not None and not math.isnan(v) else 0 for v in afts]
        w = 0.35
        ax.bar(x - w / 2, bef_vals, w, label="Before (HM)", color="steelblue", alpha=0.8)
        ax.bar(x + w / 2, aft_vals, w, label="After (HM)", color="coral", alpha=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
        ax.set_ylabel("Satisfaction (Harmonic Mean)")
        ax.set_title("System Satisfaction Across Trials")
        ax.set_ylim(0, 1.1)
        ax.legend()
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        p = run_dir / "cross_satisfaction.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    # 5) Training time comparison
    train_times = [s.avg_training_ms for s in summaries]
    if any(v is not None for v in train_times):
        fig, ax = plt.subplots(figsize=(max(10, n * 1.2), 5))
        vals = [v if v is not None else 0 for v in train_times]
        ax.bar(x, vals, color="mediumpurple", alpha=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
        ax.set_ylabel("Avg Training Time (ms)")
        ax.set_title("Average Local Training Duration Across Trials")
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        p = run_dir / "cross_training_time.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    # 6) Trial overview table
    fig, ax = plt.subplots(figsize=(max(14, n * 2), 2 + 0.4 * n))
    ax.axis("off")
    cols = ["Trial", "Time", "Rounds", "Terminals", "Final Acc", "Final Loss",
            "Sat Before(HM)", "Sat After(HM)", "Avg Train(ms)", "Samples"]
    cell_text = []
    for s in summaries:
        t_str = s.window_start.strftime("%m/%d %H:%M") if s.window_start else "-"
        cell_text.append([
            s.trial_id,
            t_str,
            str(s.num_rounds),
            str(s.num_terminals),
            f"{s.final_accuracy:.3f}" if s.final_accuracy is not None else "-",
            f"{s.final_loss:.3f}" if s.final_loss is not None else "-",
            f"{s.hm_satisfaction_before:.3f}" if s.hm_satisfaction_before is not None and not math.isnan(s.hm_satisfaction_before) else "-",
            f"{s.hm_satisfaction_after:.3f}" if s.hm_satisfaction_after is not None and not math.isnan(s.hm_satisfaction_after) else "-",
            f"{s.avg_training_ms:.0f}" if s.avg_training_ms is not None else "-",
            str(s.total_samples) if s.total_samples else "-",
        ])
    table = ax.table(cellText=cell_text, colLabels=cols, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1, 1.4)
    for j in range(len(cols)):
        table[(0, j)].set_facecolor("#4472C4")
        table[(0, j)].set_text_props(color="white", fontweight="bold")
    ax.set_title(f"Multi-Trial Overview ({len(summaries)} trials)", fontsize=13, fontweight="bold", pad=20)
    fig.tight_layout()
    p = run_dir / "cross_trial_overview.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    names.append(p.name)

    # 7) Rounds & terminal count scatter
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    rounds = [s.num_rounds for s in summaries]
    terms = [s.num_terminals for s in summaries]
    ax1.bar(x, rounds, color="teal", alpha=0.8)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
    ax1.set_ylabel("Number of Rounds")
    ax1.set_title("Rounds per Trial")
    ax1.grid(True, alpha=0.3, axis="y")

    ax2.bar(x, terms, color="darkorange", alpha=0.8)
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
    ax2.set_ylabel("Number of Terminals")
    ax2.set_title("Terminals per Trial")
    ax2.grid(True, alpha=0.3, axis="y")
    fig.tight_layout()
    p = run_dir / "cross_rounds_terminals.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    names.append(p.name)

    # 8) Upload latency comparison
    upload_lats = [s.avg_upload_latency_ms for s in summaries]
    if any(v is not None for v in upload_lats):
        fig, ax = plt.subplots(figsize=(max(10, n * 1.2), 5))
        vals = [v if v is not None else 0 for v in upload_lats]
        ax.bar(x, vals, color="salmon", alpha=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
        ax.set_ylabel("Avg Upload Latency (ms)")
        ax.set_title("Average Upload Latency Across Trials")
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        p = run_dir / "cross_upload_latency.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    # 9) Scatter: Upload Latency / RTT vs Satisfaction After
    # Collect all data points across trials
    scatter_rows = []
    for s in summaries:
        if s.df is None:
            continue
        df_v = s.df.dropna(subset=["round_id"])
        # Upload latency from upload events
        udf = alt._upload_timing_dataframe(s.rep) if s.rep else None
        if udf is not None and not udf.empty:
            merged = df_v.merge(udf[["terminal_id", "round_id", "upload_duration_ms"]],
                                on=["terminal_id", "round_id"], how="left")
        else:
            merged = df_v.copy()
            merged["upload_duration_ms"] = None

        # Also pull RTT from idx_df
        if s.idx_df is not None and not s.idx_df.empty and "sat_rtt_ms" in s.idx_df.columns:
            rtt_map = s.idx_df.dropna(subset=["sat_rtt_ms"]).set_index(["terminal_id", "round_id"])["sat_rtt_ms"]
            merged["rtt_ms"] = merged.apply(
                lambda r: rtt_map.get((r["terminal_id"], r["round_id"])), axis=1)
        else:
            merged["rtt_ms"] = None

        for _, row in merged.iterrows():
            sat_a = row.get("satisfaction_after")
            if sat_a is None or (isinstance(sat_a, float) and math.isnan(sat_a)):
                # Use satisfaction_before as fallback for scatter
                sat_a = row.get("satisfaction_before")
            if sat_a is None or (isinstance(sat_a, float) and math.isnan(sat_a)):
                continue
            ul = row.get("upload_duration_ms")
            rtt = row.get("rtt_ms")
            if ul is not None and not (isinstance(ul, float) and math.isnan(ul)):
                scatter_rows.append({"latency_type": "Upload Latency (ms)", "latency": float(ul),
                                     "satisfaction": float(sat_a), "trial": s.trial_id,
                                     "terminal": row.get("terminal_id")})
            if rtt is not None and not (isinstance(rtt, float) and math.isnan(rtt)):
                scatter_rows.append({"latency_type": "RTT (ms)", "latency": float(rtt),
                                     "satisfaction": float(sat_a), "trial": s.trial_id,
                                     "terminal": row.get("terminal_id")})

    if scatter_rows:
        import pandas as pd
        sdf = pd.DataFrame(scatter_rows)
        latency_types = sdf["latency_type"].unique()
        fig, axes = plt.subplots(1, len(latency_types), figsize=(7 * len(latency_types), 6), squeeze=False)
        axes = axes.flatten()
        for ax, lt in zip(axes, latency_types):
            sub = sdf[sdf["latency_type"] == lt]
            ax.scatter(sub["latency"], sub["satisfaction"], alpha=0.6, edgecolors="k", linewidth=0.3, s=60)
            # Trend line
            if len(sub) >= 3:
                z = np.polyfit(sub["latency"], sub["satisfaction"], 1)
                px = np.linspace(sub["latency"].min(), sub["latency"].max(), 50)
                ax.plot(px, np.polyval(z, px), color="red", linewidth=2, linestyle="--",
                        label=f"trend (slope={z[0]:.4f})")
                ax.legend(fontsize=9)
            ax.set_xlabel(lt)
            ax.set_ylabel("Satisfaction")
            ax.set_title(f"Communication Load vs Satisfaction")
            ax.grid(True, alpha=0.3)
        fig.suptitle("Communication Load Impact on QoS Satisfaction", fontsize=13, fontweight="bold")
        fig.tight_layout()
        p = run_dir / "cross_latency_vs_satisfaction.png"
        fig.savefig(p, dpi=150, bbox_inches="tight")
        plt.close(fig)
        names.append(p.name)

    return names


# ---------------------------------------------------------------------------
# Summary Dashboard (presentation-grade plots)
# ---------------------------------------------------------------------------
def _filter_successful_trials(summaries: List[TrialSummary],
                              min_rounds: int = 5) -> List[TrialSummary]:
    """Filter to successful trials only: rounds >= min_rounds and non-null final accuracy."""
    return [s for s in summaries
            if s.num_rounds >= min_rounds
            and s.final_accuracy is not None
            and not math.isnan(s.final_accuracy)]


def write_summary_dashboard(summaries: List[TrialSummary], run_dir: Path) -> List[str]:
    """Generate presentation-grade Summary Dashboard plots (5 items from policy)."""
    plt = alt._setup_matplotlib()
    import numpy as np
    import pandas as pd
    names = []

    # 1) Filter to successful trials
    valid = _filter_successful_trials(summaries)
    if not valid:
        print("  WARN: No successful trials for Summary Dashboard (need rounds>=5, non-null acc)")
        return names

    n_total = len(summaries)
    n_valid = len(valid)
    if n_valid < n_total:
        excluded = n_total - n_valid
        print(f"  Summary Dashboard: {excluded} trial(s) excluded (incomplete), using {n_valid} trial(s)")

    # ---- 2) Aggregated Accuracy Curve (Mean + 1 std shading) ----
    all_acc_series = []
    for s in valid:
        if s.df is None:
            continue
        df_v = s.df.dropna(subset=["round_id", "accuracy"])
        if df_v.empty:
            continue
        # Per-trial: mean accuracy at each round
        trial_means = df_v.groupby("round_id")["accuracy"].mean().sort_index()
        all_acc_series.append(trial_means)

    if all_acc_series:
        # Align all series to a common round index
        combined = pd.concat(all_acc_series, axis=1)
        grand_mean = combined.mean(axis=1)
        grand_std = combined.std(axis=1).fillna(0)
        rx = grand_mean.index.astype(float)

        fig, ax = plt.subplots(figsize=(10, 6))
        ax.plot(rx, grand_mean.values, color="#2C5F8A", linewidth=3, marker="D",
                markersize=6, label=f"Mean ({n_valid} trials)")
        ax.fill_between(rx, (grand_mean - grand_std).values, (grand_mean + grand_std).values,
                        color="#2C5F8A", alpha=0.18, label="1 std")
        ax.set_xlabel("Round", fontsize=12)
        ax.set_ylabel("Accuracy", fontsize=12)
        ax.set_title("HFL System Stability (Mean Accuracy)", fontsize=14, fontweight="bold")
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        p = run_dir / "summary_accuracy_stability.png"
        fig.savefig(p, dpi=150)
        plt.close(fig)
        names.append(p.name)

    # ---- 3) QoS Impact Summary: Before vs After, 2 bars with % decrease ----
    all_bef = []
    all_aft = []
    for s in valid:
        if s.hm_satisfaction_before is not None and not math.isnan(s.hm_satisfaction_before):
            all_bef.append(s.hm_satisfaction_before)
        if s.hm_satisfaction_after is not None and not math.isnan(s.hm_satisfaction_after):
            all_aft.append(s.hm_satisfaction_after)

    if all_bef and all_aft:
        mean_bef = np.mean(all_bef)
        mean_aft = np.mean(all_aft)
        pct_change = (mean_aft - mean_bef) / mean_bef * 100 if mean_bef != 0 else 0

        fig, ax = plt.subplots(figsize=(6, 6))
        bar_colors = ["#4472C4", "#ED7D31"]
        bars = ax.bar(["Before Training", "After Training"],
                      [mean_bef, mean_aft], color=bar_colors, width=0.5, alpha=0.9,
                      edgecolor="black", linewidth=0.8)
        # Annotate values on bars
        for bar, val in zip(bars, [mean_bef, mean_aft]):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.01,
                    f"{val:.3f}", ha="center", va="bottom", fontsize=12, fontweight="bold")
        # Annotate percentage change
        sign = "+" if pct_change >= 0 else ""
        change_color = "green" if pct_change >= 0 else "red"
        ax.annotate(f"{sign}{pct_change:.1f}%",
                    xy=(1, mean_aft), xytext=(1.35, (mean_bef + mean_aft) / 2),
                    fontsize=16, fontweight="bold", color=change_color,
                    arrowprops=dict(arrowstyle="->", color=change_color, lw=2),
                    ha="center", va="center")
        ax.set_ylabel("Satisfaction (Harmonic Mean)", fontsize=12)
        ax.set_title("QoS Impact Summary", fontsize=14, fontweight="bold")
        ax.set_ylim(0, max(mean_bef, mean_aft) * 1.25)
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        p = run_dir / "summary_qos_impact.png"
        fig.savefig(p, dpi=150)
        plt.close(fig)
        names.append(p.name)

    # ---- 4) Scatter: Network Load (Latency) vs User Satisfaction ----
    scatter_rows = []
    for s in valid:
        if s.df is None:
            continue
        df_v = s.df.dropna(subset=["round_id"])
        udf = alt._upload_timing_dataframe(s.rep) if s.rep else None
        if udf is not None and not udf.empty:
            merged = df_v.merge(udf[["terminal_id", "round_id", "upload_duration_ms"]],
                                on=["terminal_id", "round_id"], how="left")
        else:
            merged = df_v.copy()
            merged["upload_duration_ms"] = None

        if s.idx_df is not None and not s.idx_df.empty and "sat_rtt_ms" in s.idx_df.columns:
            rtt_map = s.idx_df.dropna(subset=["sat_rtt_ms"]).set_index(["terminal_id", "round_id"])["sat_rtt_ms"]
            merged["rtt_ms"] = merged.apply(
                lambda r: rtt_map.get((r["terminal_id"], r["round_id"])), axis=1)
        else:
            merged["rtt_ms"] = None

        for _, row in merged.iterrows():
            sat_a = row.get("satisfaction_after")
            if sat_a is None or (isinstance(sat_a, float) and math.isnan(sat_a)):
                sat_a = row.get("satisfaction_before")
            if sat_a is None or (isinstance(sat_a, float) and math.isnan(sat_a)):
                continue
            # Use RTT as primary latency, fallback to upload duration
            latency = None
            rtt = row.get("rtt_ms")
            ul = row.get("upload_duration_ms")
            if rtt is not None and not (isinstance(rtt, float) and math.isnan(rtt)):
                latency = float(rtt)
            elif ul is not None and not (isinstance(ul, float) and math.isnan(ul)):
                latency = float(ul)
            if latency is not None:
                scatter_rows.append({"latency": latency, "satisfaction": float(sat_a)})

    if scatter_rows:
        sdf = pd.DataFrame(scatter_rows)
        fig, ax = plt.subplots(figsize=(8, 6))
        ax.scatter(sdf["latency"], sdf["satisfaction"], alpha=0.5, edgecolors="k",
                   linewidth=0.3, s=60, color="#4472C4")
        if len(sdf) >= 3:
            z = np.polyfit(sdf["latency"], sdf["satisfaction"], 1)
            px = np.linspace(sdf["latency"].min(), sdf["latency"].max(), 50)
            ax.plot(px, np.polyval(z, px), color="red", linewidth=2, linestyle="--",
                    label=f"trend (slope={z[0]:.4f})")
            ax.legend(fontsize=10)
        # Simplified axis labels
        ax.set_xlabel("Network Load (Latency)", fontsize=12)
        ax.set_ylabel("User Satisfaction", fontsize=12)
        ax.set_title("Load vs Satisfaction Correlation", fontsize=14, fontweight="bold")
        # Annotation in whitespace
        ax.text(0.95, 0.05,
                "Lower right: Communication load\nimpairs UX",
                transform=ax.transAxes, fontsize=10, va="bottom", ha="right",
                style="italic", color="gray",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="lightyellow", alpha=0.8))
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        p = run_dir / "summary_load_vs_satisfaction.png"
        fig.savefig(p, dpi=150)
        plt.close(fig)
        names.append(p.name)

    # ---- 5) Summary table: 1 row with Mean +/- Std ----
    metrics = {
        "Final Acc": [s.final_accuracy for s in valid if s.final_accuracy is not None],
        "Final Loss": [s.final_loss for s in valid if s.final_loss is not None],
        "Sat Before": [s.hm_satisfaction_before for s in valid
                       if s.hm_satisfaction_before is not None and not math.isnan(s.hm_satisfaction_before)],
        "Sat After": [s.hm_satisfaction_after for s in valid
                      if s.hm_satisfaction_after is not None and not math.isnan(s.hm_satisfaction_after)],
        "Train(ms)": [s.avg_training_ms for s in valid if s.avg_training_ms is not None],
        "Rounds": [float(s.num_rounds) for s in valid],
    }
    cols = []
    mean_row = []
    for k, vals in metrics.items():
        if not vals:
            continue
        cols.append(k)
        m = np.mean(vals)
        sd = np.std(vals, ddof=1) if len(vals) > 1 else 0
        mean_row.append(f"{m:.3f} +/- {sd:.3f}")

    if cols:
        fig, ax = plt.subplots(figsize=(max(10, len(cols) * 2), 2))
        ax.axis("off")
        table = ax.table(cellText=[mean_row], colLabels=cols,
                         rowLabels=[f"Mean (n={n_valid})"],
                         loc="center", cellLoc="center")
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1, 1.8)
        for j in range(len(cols)):
            table[(0, j)].set_facecolor("#2C5F8A")
            table[(0, j)].set_text_props(color="white", fontweight="bold")
        table[(1, -1)].set_facecolor("#E8EEF4")
        table[(1, -1)].set_text_props(fontweight="bold")
        ax.set_title(f"Summary Statistics ({n_valid} successful trials)",
                     fontsize=13, fontweight="bold", pad=15)
        fig.tight_layout()
        p = run_dir / "summary_table.png"
        fig.savefig(p, dpi=150, bbox_inches="tight")
        plt.close(fig)
        names.append(p.name)

    return names


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------
def build_cross_trial_markdown(summaries: List[TrialSummary], plot_files: List[str],
                               summary_plot_files: Optional[List[str]] = None) -> str:
    lines = []
    lines.append(f"# Multi-Trial Analysis Report ({len(summaries)} trials)")
    lines.append("")
    lines.append(f"Generated: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`")
    lines.append("")

    lines.append("## Trial Summary Table")
    lines.append("| # | Trial ID | Time | Rounds | Terminals | Final Acc | Final Loss | Sat Before(HM) | Sat After(HM) | Avg Train(ms) |")
    lines.append("|---|----------|------|--------|-----------|-----------|------------|----------------|---------------|---------------|")
    for i, s in enumerate(summaries, 1):
        t_str = s.window_start.strftime("%m/%d %H:%M") if s.window_start else "-"
        acc = f"{s.final_accuracy:.3f}" if s.final_accuracy is not None else "-"
        loss = f"{s.final_loss:.3f}" if s.final_loss is not None else "-"
        sb = f"{s.hm_satisfaction_before:.3f}" if s.hm_satisfaction_before is not None and not math.isnan(s.hm_satisfaction_before) else "-"
        sa = f"{s.hm_satisfaction_after:.3f}" if s.hm_satisfaction_after is not None and not math.isnan(s.hm_satisfaction_after) else "-"
        ms = f"{s.avg_training_ms:.0f}" if s.avg_training_ms is not None else "-"
        lines.append(f"| {i} | {s.trial_id} | {t_str} | {s.num_rounds} | {s.num_terminals} | {acc} | {loss} | {sb} | {sa} | {ms} |")
    lines.append("")

    if plot_files:
        lines.append("## Cross-Trial Comparison Plots")
        for fn in plot_files:
            lines.append(f"![{fn}]({fn})")
        lines.append("")

    if summary_plot_files:
        lines.append("## Summary Dashboard (Presentation)")
        lines.append("")
        valid = _filter_successful_trials(summaries)
        lines.append(f"*Successful trials: {len(valid)}/{len(summaries)} "
                      f"(filter: rounds >= 5, non-null Final Acc)*")
        lines.append("")
        for fn in summary_plot_files:
            lines.append(f"![{fn}]({fn})")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Trial selection
# ---------------------------------------------------------------------------
def list_all_central_logs(min_lines: int = 100) -> List[Path]:
    logs_dir = ROOT / "logs" / "time_records"
    candidates = sorted(logs_dir.glob("central_server_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for p in candidates:
        try:
            n = sum(1 for _ in open(p, encoding="utf-8", errors="ignore"))
            if n >= min_lines:
                result.append(p)
        except OSError:
            continue
    return result


def interactive_select(logs: List[Path]) -> List[Path]:
    """Interactive terminal selection of trial logs."""
    print("\nAvailable trials:")
    print("-" * 80)
    for i, p in enumerate(logs, 1):
        try:
            n_lines = sum(1 for _ in open(p, encoding="utf-8", errors="ignore"))
        except OSError:
            n_lines = 0
        w0, w1 = alt.central_time_window(p)
        t_str = w0.strftime("%Y-%m-%d %H:%M") if w0 else "?"
        dur = f"{(w1-w0).total_seconds()/60:.0f}min" if w0 and w1 else "?"
        print(f"  [{i:2d}] {p.stem}  ({t_str}, {dur}, {n_lines} lines)")
    print("-" * 80)
    print("Enter numbers separated by spaces (e.g. '1 3 5'), range (e.g. '1-5'), or 'all':")
    raw = input("> ").strip()
    if raw.lower() == "all":
        return logs
    indices = set()
    for part in raw.split():
        if "-" in part:
            a, b = part.split("-", 1)
            for i in range(int(a), int(b) + 1):
                indices.add(i)
        else:
            indices.add(int(part))
    return [logs[i - 1] for i in sorted(indices) if 1 <= i <= len(logs)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Multi-trial cross-comparison analysis")
    ap.add_argument("-n", "--last", type=int, default=None, help="Analyze the N most recent trials")
    ap.add_argument("--all", action="store_true", help="Analyze all available trials")
    ap.add_argument("--select", nargs="+", type=str, default=None, help="Specify central log paths")
    ap.add_argument("--interactive", action="store_true", help="Interactive trial selection")
    ap.add_argument("--min-lines", type=int, default=100, help="Minimum lines to consider a valid trial (default: 100)")
    ap.add_argument("--min-rounds", type=int, default=5, help="Minimum rounds required to process a trial (default: 5)")
    ap.add_argument("--out-dir", type=str, default="analysis_output", help="Base output directory")
    ap.add_argument("--json-summary", action="store_true", help="Also write JSON summary")
    ap.add_argument("--no-plots", action="store_true", help="Skip plots")
    args = ap.parse_args()

    # Determine which logs to analyze
    if args.select:
        selected = [Path(p) if Path(p).is_absolute() else (ROOT / p).resolve() for p in args.select]
    else:
        all_logs = list_all_central_logs(min_lines=args.min_lines)
        if not all_logs:
            print("No trial logs found.", file=sys.stderr)
            return 2

        if args.interactive:
            selected = interactive_select(all_logs)
        elif args.all:
            selected = all_logs
        elif args.last:
            selected = all_logs[:args.last]
        else:
            # Default: last 5
            selected = all_logs[:5]

    if not selected:
        print("No trials selected.", file=sys.stderr)
        return 2

    print(f"\nAnalyzing {len(selected)} trials...\n")

    # Process each trial
    summaries: List[TrialSummary] = []
    for i, log_path in enumerate(selected, 1):
        print(f"  [{i}/{len(selected)}] {log_path.stem} ...", end=" ", flush=True)
        ts = build_trial_summary(log_path)
        if ts is None:
            print("SKIP (no valid data)")
            continue
        if ts.num_rounds < args.min_rounds:
            print(f"SKIP (only {ts.num_rounds} rounds, requires {args.min_rounds})")
            continue
        acc_str = f"{ts.final_accuracy:.3f}" if ts.final_accuracy is not None else "N/A"
        print(f"OK (rounds={ts.num_rounds}, terminals={ts.num_terminals}, acc={acc_str})")
        summaries.append(ts)

    if not summaries:
        print("\nNo valid trials found.", file=sys.stderr)
        return 2

    # Sort by time
    summaries.sort(key=lambda s: s.window_start or datetime.min)

    # Output directory
    base_out = (ROOT / args.out_dir).resolve()
    base_out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = base_out / f"multi_trial_{stamp}_{len(summaries)}trials"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Generate cross-trial plots
    plot_files: List[str] = []
    summary_plot_files: List[str] = []
    if not args.no_plots:
        try:
            alt._setup_matplotlib()
            plot_files = write_cross_trial_plots(summaries, run_dir)
            # Summary Dashboard (presentation-grade)
            print("\n  Generating Summary Dashboard...")
            summary_plot_files = write_summary_dashboard(summaries, run_dir)
        except ImportError:
            print("WARN: matplotlib not available", file=sys.stderr)

    # Write report
    md_text = build_cross_trial_markdown(summaries, plot_files, summary_plot_files)
    md_path = run_dir / "multi_trial_analysis.md"
    md_path.write_text(md_text, encoding="utf-8")

    print(f"\nOutput: {run_dir}")
    print(f"Report: {md_path}")
    if plot_files:
        print(f"  {len(plot_files)} cross-trial plots:")
        for pf in plot_files:
            print(f"    {pf}")
    if summary_plot_files:
        print(f"  {len(summary_plot_files)} summary dashboard plots:")
        for pf in summary_plot_files:
            print(f"    {pf}")

    if args.json_summary:
        summary_data = {
            "generated": datetime.now().isoformat(),
            "num_trials": len(summaries),
            "trials": [
                {
                    "trial_id": s.trial_id,
                    "window_start": s.window_start.isoformat() if s.window_start else None,
                    "window_end": s.window_end.isoformat() if s.window_end else None,
                    "num_rounds": s.num_rounds,
                    "num_terminals": s.num_terminals,
                    "final_accuracy": s.final_accuracy,
                    "final_loss": s.final_loss,
                    "hm_satisfaction_before": s.hm_satisfaction_before if s.hm_satisfaction_before and not math.isnan(s.hm_satisfaction_before) else None,
                    "hm_satisfaction_after": s.hm_satisfaction_after if s.hm_satisfaction_after and not math.isnan(s.hm_satisfaction_after) else None,
                    "avg_training_ms": s.avg_training_ms,
                    "total_samples": s.total_samples,
                }
                for s in summaries
            ],
            "plots": plot_files,
        }
        js_path = run_dir / "multi_trial_summary.json"
        js_path.write_text(json.dumps(summary_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  JSON: {js_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
