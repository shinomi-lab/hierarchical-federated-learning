"""
各グローバルラウンドのval_acc / train_acc / val_loss をグラフ化するスクリプト。

使い方:
  python analysis/plot_accuracy.py                          # 最新ログを自動選択
  python analysis/plot_accuracy.py results/logs/rounds_xxx.jsonl
"""

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")          # GUI不要
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np


# ── ログ選択 ──────────────────────────────────────────────────────────────
def _pick_log() -> Path:
    if len(sys.argv) >= 2:
        p = Path(sys.argv[1])
        if not p.exists():
            sys.exit(f"[ERROR] ファイルが見つかりません: {p}")
        return p
    logs_dir = Path(__file__).parent.parent / "results" / "logs"
    files = sorted(logs_dir.glob("rounds_*.jsonl"), key=lambda x: x.stat().st_mtime)
    if not files:
        sys.exit("[ERROR] results/logs/ にログファイルがありません。先にシミュレーションを実行してください。")
    return files[-1]


# ── データ抽出 ─────────────────────────────────────────────────────────────
def _load(log_path: Path) -> dict:
    """
    aggregation_complete イベントから各 global_round の代表値 (local_round 最大) を抽出する。
    inference_result イベントから各 global_round の満足度平均も抽出する。
    """
    agg_rows: dict[int, dict] = {}       # g_rnd -> row (local_round 最大を優先)
    sat_rows: dict[int, list] = {}       # g_rnd -> [satisfaction values]

    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            event = ev.get("event", "")

            if event == "aggregation_complete":
                g = ev.get("global_round", ev.get("round_id", 0))
                l = ev.get("local_round", 0)
                if g not in agg_rows or l > agg_rows[g].get("local_round", 0):
                    agg_rows[g] = ev

            elif event == "inference_result":
                g = ev.get("global_round", ev.get("round_id", 0))
                sat = ev.get("satisfaction")
                if sat is not None:
                    sat_rows.setdefault(g, []).append(sat)

    rounds      = sorted(agg_rows.keys())
    val_acc     = [agg_rows[r]["avg_val_acc"] * 100  for r in rounds]
    train_acc   = [agg_rows[r]["avg_train_acc"] * 100 for r in rounds]
    val_loss    = [agg_rows[r]["avg_val_loss"]        for r in rounds]
    satisfaction = [
        np.mean(sat_rows[r]) * 100 if r in sat_rows else None
        for r in rounds
    ]

    return {
        "log_path":     log_path,
        "rounds":       rounds,
        "val_acc":      val_acc,
        "train_acc":    train_acc,
        "val_loss":     val_loss,
        "satisfaction": satisfaction,
    }


# ── プロット ────────────────────────────────────────────────────────────────
def plot(data: dict, out_dir: Path | None = None) -> Path:
    rounds      = data["rounds"]
    val_acc     = data["val_acc"]
    train_acc   = data["train_acc"]
    val_loss    = data["val_loss"]
    satisfaction = data["satisfaction"]

    # 収束ラウンド (val_acc が初めて 90% を超えたラウンド)
    conv_rnd = next((r for r, a in zip(rounds, val_acc) if a >= 90.0), None)

    fig, axes = plt.subplots(3, 1, figsize=(10, 12), constrained_layout=True)
    fig.suptitle(
        f"HFL Simulation — Accuracy / Loss / Satisfaction\n({data['log_path'].name})",
        fontsize=13, fontweight="bold"
    )

    colors = {"val_acc": "#2196F3", "train_acc": "#FF9800", "val_loss": "#F44336", "sat": "#4CAF50"}

    # ─ subplot 1: Accuracy ─────────────────────────────────────────────────
    ax1 = axes[0]
    ax1.plot(rounds, val_acc,   color=colors["val_acc"],   lw=2.0, marker="o", ms=4, label="Val Acc")
    ax1.plot(rounds, train_acc, color=colors["train_acc"], lw=1.5, marker="s", ms=3, ls="--", label="Train Acc")
    ax1.fill_between(rounds, train_acc, val_acc, alpha=0.08, color=colors["val_acc"])
    if conv_rnd is not None:
        conv_acc = val_acc[rounds.index(conv_rnd)]
        ax1.axvline(conv_rnd, color="gray", lw=1.2, ls=":", alpha=0.8)
        ax1.annotate(
            f"Converged Rnd {conv_rnd}\n({conv_acc:.1f}%)",
            xy=(conv_rnd, conv_acc), xytext=(conv_rnd + 0.8, conv_acc - 8),
            arrowprops=dict(arrowstyle="->", color="gray"), fontsize=9, color="gray"
        )
    ax1.axhline(90, color="gray", lw=0.8, ls="--", alpha=0.5)
    ax1.set_ylabel("Accuracy (%)", fontsize=11)
    ax1.set_ylim(0, 105)
    ax1.set_xlim(rounds[0] - 0.5, rounds[-1] + 0.5)
    ax1.xaxis.set_major_locator(ticker.MultipleLocator(5))
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc="lower right", fontsize=10)

    # 最終値アノテーション
    ax1.annotate(
        f"{val_acc[-1]:.1f}%",
        xy=(rounds[-1], val_acc[-1]),
        xytext=(rounds[-1] - 2, val_acc[-1] + 3),
        fontsize=9, color=colors["val_acc"], fontweight="bold"
    )

    # ─ subplot 2: Loss ─────────────────────────────────────────────────────
    ax2 = axes[1]
    ax2.plot(rounds, val_loss, color=colors["val_loss"], lw=2.0, marker="o", ms=4, label="Val Loss")
    ax2.fill_between(rounds, val_loss, alpha=0.12, color=colors["val_loss"])
    ax2.set_ylabel("Loss", fontsize=11)
    ax2.set_xlim(rounds[0] - 0.5, rounds[-1] + 0.5)
    ax2.xaxis.set_major_locator(ticker.MultipleLocator(5))
    ax2.grid(True, alpha=0.3)
    ax2.legend(loc="upper right", fontsize=10)

    # ─ subplot 3: Satisfaction ─────────────────────────────────────────────
    ax3 = axes[2]
    sat_vals = [s for s in satisfaction if s is not None]
    sat_rnds = [r for r, s in zip(rounds, satisfaction) if s is not None]
    if sat_vals:
        ax3.bar(sat_rnds, sat_vals, color=colors["sat"], alpha=0.7, width=0.6, label="Avg Satisfaction")
        ax3.axhline(np.mean(sat_vals), color="darkgreen", lw=1.5, ls="--",
                    label=f"Mean {np.mean(sat_vals):.1f}%")
        ax3.set_ylim(0, 110)
        ax3.legend(loc="lower right", fontsize=10)
    else:
        ax3.text(0.5, 0.5, "satisfaction データなし", ha="center", va="center",
                 transform=ax3.transAxes, color="gray")
    ax3.set_xlabel("Global Round", fontsize=11)
    ax3.set_ylabel("Terminal Satisfaction (%)", fontsize=11)
    ax3.set_xlim(rounds[0] - 0.5, rounds[-1] + 0.5)
    ax3.xaxis.set_major_locator(ticker.MultipleLocator(5))
    ax3.grid(True, alpha=0.3, axis="y")

    # ── 保存 ─────────────────────────────────────────────────────────────
    if out_dir is None:
        out_dir = data["log_path"].parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = data["log_path"].stem.replace("rounds_", "")
    out_path = out_dir / f"accuracy_{stem}.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path


# ── エントリポイント ──────────────────────────────────────────────────────
if __name__ == "__main__":
    log_path = _pick_log()
    print(f"[INFO] ログ: {log_path}")
    data = _load(log_path)
    print(f"[INFO] 抽出ラウンド数: {len(data['rounds'])}  "
          f"val_acc 範囲: {min(data['val_acc']):.1f}% ~ {max(data['val_acc']):.1f}%")
    out = plot(data)
    print(f"[OK]  グラフ保存: {out}")
