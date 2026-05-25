#!/usr/bin/env python3
"""
シミュレーション vs 実機 比較グラフ生成スクリプト

生成物（out_dir に出力）:
  separate_sim.png        — シミュレーション単体: val_acc / val_loss / satisfaction
  separate_real.png       — 実機単体: satisfaction (avg/min/harmonic) + local training time
  combined_satisfaction.png — 重ね合わせ: sim と実機の satisfaction per round

使い方:
  cd Serverside_HFL

  # 最新ログを自動選択
  python3 scripts/compare_sim_real.py

  # 明示指定
  python3 scripts/compare_sim_real.py \\
      --sim-log     ../Sim_HFL/results/logs/rounds_XXXX.jsonl \\
      --real-central logs/time_records/central_server_YYYYMMDD_HHMMSS.log \\
      --out-dir     analysis_output/comparison
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

# ── パス定数 ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve()
SERVERSIDE_ROOT = _HERE.parents[1]           # Serverside_HFL/
HFL_ROOT        = SERVERSIDE_ROOT.parent     # HFL/
SIM_LOGS_DIR    = HFL_ROOT / "Sim_HFL" / "results" / "logs"
REAL_LOGS_DIR   = SERVERSIDE_ROOT / "logs" / "time_records"

# ── 共通色設定 ──────────────────────────────────────────────────────────────
C_SIM_ACC   = "#2196F3"   # 青
C_SIM_TRAIN = "#FF9800"   # オレンジ
C_SIM_LOSS  = "#F44336"   # 赤
C_SIM_SAT   = "#4CAF50"   # 緑
C_REAL_SAT  = "#9C27B0"   # 紫
C_REAL_TIME = "#795548"   # 茶


# ══════════════════════════════════════════════════════════════════════════════
# シミュレーションデータ読み込み
# ══════════════════════════════════════════════════════════════════════════════

def _pick_sim_log() -> Optional[Path]:
    if not SIM_LOGS_DIR.exists():
        return None
    files = sorted(SIM_LOGS_DIR.glob("rounds_*.jsonl"), key=lambda x: x.stat().st_mtime)
    return files[-1] if files else None


def load_sim_data(log_path: Path) -> dict:
    """
    aggregation_complete と inference_result から各グローバルラウンドの代表値を抽出する。
    plot_accuracy.py の _load() と同等のロジック。
    """
    agg_rows: dict[int, dict] = {}
    sat_rows: dict[int, list] = {}

    with log_path.open(encoding="utf-8", errors="ignore") as f:
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
                g = ev.get("global_round") or ev.get("round_id", 0)
                l = ev.get("local_round", 0)
                if g not in agg_rows or l > agg_rows[g].get("local_round", 0):
                    agg_rows[g] = ev

            elif event == "inference_result":
                g = ev.get("global_round") or ev.get("round_id", 0)
                sat = ev.get("satisfaction")
                if sat is not None:
                    sat_rows.setdefault(g, []).append(float(sat))

    rounds    = sorted(agg_rows.keys())
    val_acc   = [agg_rows[r].get("avg_val_acc",   0.0) * 100 for r in rounds]
    train_acc = [agg_rows[r].get("avg_train_acc", 0.0) * 100 for r in rounds]
    val_loss  = [agg_rows[r].get("avg_val_loss",  0.0)       for r in rounds]
    sat_avg   = [
        float(np.mean(sat_rows[r])) if r in sat_rows else None
        for r in rounds
    ]

    return {
        "log_path":  log_path,
        "rounds":    rounds,
        "val_acc":   val_acc,
        "train_acc": train_acc,
        "val_loss":  val_loss,
        "sat_avg":   sat_avg,          # 0–1 スケール
    }


# ══════════════════════════════════════════════════════════════════════════════
# 実機データ読み込み
# ══════════════════════════════════════════════════════════════════════════════

_BRACKET_TS = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _parse_ts(s: str) -> Optional[datetime]:
    s = s.strip()[:19]
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _find_latest_central() -> Optional[Path]:
    files = sorted(REAL_LOGS_DIR.glob("central_server_*.log"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _central_window(central: Path) -> Tuple[Optional[datetime], Optional[datetime]]:
    t0, t1 = None, None
    try:
        text = central.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, None
    for m in _BRACKET_TS.finditer(text):
        t = _parse_ts(m.group(1))
        if t is None:
            continue
        if t0 is None or t < t0:
            t0 = t
        if t1 is None or t > t1:
            t1 = t
    return t0, t1


def _iter_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _cm_dict(raw) -> dict:
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


def load_real_data(central: Path) -> dict:
    """
    central ログの時刻窓内にある edge_server JSONL から
    telemetry_received イベントを読んで satisfaction / 学習時間を抽出する。
    """
    w0, w1 = _central_window(central)
    if w0 and w1:
        w0_ext = w0 - timedelta(minutes=5)
        w1_ext = w1 + timedelta(minutes=5)
    else:
        w0_ext = w1_ext = None

    # edge_server ログを列挙
    edge_logs: List[Path] = []
    for base in (REAL_LOGS_DIR, SERVERSIDE_ROOT / "logs" / "rounds"):
        if not base.exists():
            continue
        for p in base.rglob("edge_server*.log"):
            if "organized" in str(p):
                continue
            edge_logs.append(p)
    edge_logs = sorted(set(edge_logs))

    # telemetry 行を収集
    # key: (terminal_id, round_id)  value: {satisfaction_before, satisfaction_after, total_local_ms}
    records: Dict[Tuple, dict] = {}

    for path in edge_logs:
        for row in _iter_jsonl(path):
            if row.get("event") != "telemetry_received":
                continue
            ts_raw = str(row.get("ts", "")).replace("T", " ")
            ts = _parse_ts(ts_raw)
            if w0_ext and w1_ext and ts and (ts < w0_ext or ts > w1_ext):
                continue

            det = row.get("details") or {}
            tid = det.get("terminal_id") or row.get("terminal_id")
            rnd = det.get("round_id")    or row.get("round_id")
            if not tid or rnd is None:
                continue

            cm = _cm_dict(det.get("client_meta") or row.get("client_meta") or {})
            sat_b = cm.get("satisfaction_before")
            sat_a = cm.get("satisfaction_after")
            ms    = cm.get("total_local_ms")

            key = (tid, int(rnd))
            records[key] = {
                "terminal_id": tid,
                "round_id":    int(rnd),
                "sat_before":  float(sat_b) if sat_b is not None else None,
                "sat_after":   float(sat_a) if sat_a is not None else None,
                "total_ms":    float(ms)    if ms    is not None else None,
            }

    all_rows = list(records.values())

    # ── post-switch テレメトリから satisfaction_after を補完 ──────────────────
    # AP 切替後に TelemetrySender が /api/v1/telemetry へ送った値を
    # received_files/terminal_telemetry/*/index.jsonl から読む。
    telemetry_dir = SERVERSIDE_ROOT / "received_files" / "terminal_telemetry"
    if telemetry_dir.exists():
        w0_ms = int(w0.timestamp() * 1000) - 5 * 60 * 1000 if w0 else None
        w1_ms = int(w1.timestamp() * 1000) + 5 * 60 * 1000 if w1 else None
        after_map: Dict[Tuple, float] = {}
        for idx_path in telemetry_dir.rglob("index.jsonl"):
            for ev in _iter_jsonl(idx_path):
                sa = ev.get("terminal_satisfaction_after")
                if sa is None:
                    continue
                ts_ms = ev.get("server_received_ts_ms") or ev.get("timestamp_ms")
                if ts_ms is not None and w0_ms and w1_ms:
                    if int(ts_ms) < w0_ms or int(ts_ms) > w1_ms:
                        continue
                tid = ev.get("terminal_id") or ev.get("device_id")
                rnd = ev.get("current_round_local") or ev.get("last_applied_round")
                if tid and rnd is not None:
                    after_map[(str(tid), int(rnd))] = float(sa)
        if after_map:
            for r in all_rows:
                if r["sat_after"] is None:
                    v = after_map.get((str(r["terminal_id"]), int(r["round_id"])))
                    if v is not None:
                        r["sat_after"] = v

    # ラウンドごとに集計
    by_rnd: Dict[int, List[dict]] = defaultdict(list)
    for r in all_rows:
        by_rnd[r["round_id"]].append(r)

    rounds = sorted(by_rnd.keys())
    _eps = 1e-9

    def _hm(vals):
        if not vals:
            return None
        try:
            return len(vals) / sum(1.0 / max(v, _eps) for v in vals)
        except Exception:
            return None

    sat_avg  = []
    sat_hm   = []
    sat_min  = []
    time_per_terminal: Dict[str, List[Tuple[int, float]]] = defaultdict(list)  # tid -> [(rnd, ms)]

    for rnd in rounds:
        recs = by_rnd[rnd]
        afts = [r["sat_after"] for r in recs if r["sat_after"] is not None]
        sat_avg.append(float(np.mean(afts)) if afts else None)
        sat_hm.append(_hm(afts))
        sat_min.append(min(afts) if afts else None)
        for r in recs:
            if r["total_ms"] is not None:
                time_per_terminal[r["terminal_id"]].append((rnd, r["total_ms"]))

    return {
        "central":           central,
        "rounds":            rounds,
        "sat_avg":           sat_avg,   # 0–1
        "sat_hm":            sat_hm,
        "sat_min":           sat_min,
        "time_per_terminal": dict(time_per_terminal),
        "all_rows":          all_rows,
    }


# ══════════════════════════════════════════════════════════════════════════════
# グラフ描画
# ══════════════════════════════════════════════════════════════════════════════

def _set_round_axis(ax, rounds):
    ax.set_xlim(rounds[0] - 0.5, rounds[-1] + 0.5)
    span = rounds[-1] - rounds[0]
    step = max(1, span // 10)
    ax.xaxis.set_major_locator(ticker.MultipleLocator(step))
    ax.grid(True, alpha=0.3)


# ── 1. シミュレーション単体グラフ ──────────────────────────────────────────
def plot_separate_sim(sim: dict, out_dir: Path) -> Path:
    rounds    = sim["rounds"]
    val_acc   = sim["val_acc"]
    train_acc = sim["train_acc"]
    val_loss  = sim["val_loss"]
    sat_avg   = sim["sat_avg"]

    n_panels = 3 if any(s is not None for s in sat_avg) else 2
    fig, axes = plt.subplots(n_panels, 1, figsize=(10, 4 * n_panels), constrained_layout=True)
    fig.suptitle(
        f"Simulation — Accuracy / Loss / Satisfaction\n({sim['log_path'].name})",
        fontsize=12, fontweight="bold"
    )

    # Accuracy
    ax = axes[0]
    ax.plot(rounds, val_acc,   color=C_SIM_ACC,   lw=2, marker="o", ms=4, label="Val Acc")
    ax.plot(rounds, train_acc, color=C_SIM_TRAIN, lw=1.5, marker="s", ms=3, ls="--", label="Train Acc")
    ax.fill_between(rounds, train_acc, val_acc, alpha=0.08, color=C_SIM_ACC)
    ax.axhline(90, color="gray", lw=0.8, ls="--", alpha=0.5)
    conv = next((r for r, a in zip(rounds, val_acc) if a >= 90.0), None)
    if conv is not None:
        ax.axvline(conv, color="gray", lw=1, ls=":", alpha=0.7)
        ax.annotate(f"Conv Rnd {conv}", xy=(conv, 90), xytext=(conv + 0.5, 83),
                    fontsize=8, color="gray", arrowprops=dict(arrowstyle="->", color="gray"))
    ax.set_ylabel("Accuracy (%)")
    ax.set_ylim(0, 105)
    ax.legend(loc="lower right", fontsize=9)
    _set_round_axis(ax, rounds)

    # Loss
    ax = axes[1]
    ax.plot(rounds, val_loss, color=C_SIM_LOSS, lw=2, marker="o", ms=4, label="Val Loss")
    ax.fill_between(rounds, val_loss, alpha=0.12, color=C_SIM_LOSS)
    ax.set_ylabel("Loss")
    ax.legend(loc="upper right", fontsize=9)
    _set_round_axis(ax, rounds)

    # Satisfaction
    if n_panels == 3:
        ax = axes[2]
        sat_vals = [s for s in sat_avg if s is not None]
        sat_rnds = [r for r, s in zip(rounds, sat_avg) if s is not None]
        if sat_vals:
            ax.bar(sat_rnds, [v * 100 for v in sat_vals],
                   color=C_SIM_SAT, alpha=0.7, width=0.6, label="Avg Satisfaction")
            ax.axhline(np.mean(sat_vals) * 100, color="darkgreen", lw=1.5, ls="--",
                       label=f"Mean {np.mean(sat_vals)*100:.1f}%")
            ax.set_ylim(0, 110)
        ax.set_ylabel("Avg Satisfaction (%)")
        ax.set_xlabel("Global Round")
        ax.legend(fontsize=9)
        _set_round_axis(ax, rounds)
        ax.grid(True, alpha=0.3, axis="y")

    out = out_dir / "separate_sim.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


# ── 2. 実機単体グラフ ────────────────────────────────────────────────────────
def plot_separate_real(real: dict, out_dir: Path) -> Path:
    rounds  = real["rounds"]
    if not rounds:
        print("WARN: 実機データに rounds がありません。separate_real.png をスキップします。", file=sys.stderr)
        return out_dir / "separate_real_empty.png"

    has_sat  = any(v is not None for v in real["sat_avg"])
    has_time = bool(real["time_per_terminal"])
    n_panels = (1 if has_sat else 0) + (1 if has_time else 0)
    if n_panels == 0:
        print("WARN: 実機データに描画できる指標がありません。", file=sys.stderr)
        return out_dir / "separate_real_empty.png"

    fig, axes_raw = plt.subplots(n_panels, 1, figsize=(10, 4 * n_panels), constrained_layout=True)
    axes = [axes_raw] if n_panels == 1 else list(axes_raw)
    fig.suptitle(
        f"Real Device — Satisfaction / Training Time\n({real['central'].name})",
        fontsize=12, fontweight="bold"
    )

    panel = 0

    # Satisfaction
    if has_sat:
        ax = axes[panel]; panel += 1
        sat_rnds = [r for r, v in zip(rounds, real["sat_avg"])  if v is not None]
        avgs  = [v * 100 for v in real["sat_avg"]  if v is not None]
        hms   = [v * 100 for v in real["sat_hm"]   if v is not None]
        mins  = [v * 100 for v in real["sat_min"]  if v is not None]

        ax.plot(sat_rnds, avgs, color=C_REAL_SAT, lw=2, marker="o", ms=5, label="Avg sat_after")
        if hms:
            ax.plot(sat_rnds, hms, color=C_REAL_SAT, lw=1.5, marker="s", ms=4, ls="--",
                    alpha=0.8, label="Harmonic mean")
        if mins:
            ax.plot(sat_rnds, mins, color=C_REAL_SAT, lw=1.2, marker="v", ms=4, ls=":",
                    alpha=0.7, label="Min")
            ax.fill_between(sat_rnds, mins, avgs, alpha=0.10, color=C_REAL_SAT)
        ax.set_ylabel("Satisfaction (%)")
        ax.set_ylim(0, 110)
        ax.legend(fontsize=9)
        _set_round_axis(ax, rounds)

    # Training time
    if has_time:
        ax = axes[panel]; panel += 1
        for tid, pts in sorted(real["time_per_terminal"].items()):
            pts_sorted = sorted(pts, key=lambda x: x[0])
            xs = [p[0] for p in pts_sorted]
            ys = [p[1] for p in pts_sorted]
            ax.plot(xs, ys, marker="o", ms=4, lw=1.5, label=tid)
        ax.set_ylabel("total_local_ms")
        ax.set_xlabel("Round")
        ax.legend(fontsize=8)
        _set_round_axis(ax, rounds)

    out = out_dir / "separate_real.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


# ── 3. 重ね合わせグラフ ──────────────────────────────────────────────────────
def plot_combined_satisfaction(sim: dict, real: dict, out_dir: Path) -> Path:
    sim_rnds  = sim["rounds"]
    sim_sat   = [v * 100 if v is not None else None for v in sim["sat_avg"]]

    real_rnds  = real["rounds"]
    real_sat   = [v * 100 if v is not None else None for v in real["sat_avg"]]
    real_sat_hm = [v * 100 if v is not None else None for v in real["sat_hm"]]

    has_sim  = any(v is not None for v in sim_sat)
    has_real = any(v is not None for v in real_sat)

    fig, ax = plt.subplots(figsize=(11, 5), constrained_layout=True)
    fig.suptitle("Simulation vs Real Device — Satisfaction per Round",
                 fontsize=13, fontweight="bold")

    if has_sim:
        xs = [r for r, v in zip(sim_rnds,  sim_sat) if v is not None]
        ys = [v for v in sim_sat if v is not None]
        ax.plot(xs, ys, color=C_SIM_SAT, lw=2, marker="o", ms=5, label="Sim avg satisfaction")

    if has_real:
        xs_r = [r for r, v in zip(real_rnds, real_sat) if v is not None]
        ys_r = [v for v in real_sat if v is not None]
        ax.plot(xs_r, ys_r, color=C_REAL_SAT, lw=2, marker="^", ms=6, label="Real avg sat_after")

        xs_hm = [r for r, v in zip(real_rnds, real_sat_hm) if v is not None]
        ys_hm = [v for v in real_sat_hm if v is not None]
        if xs_hm:
            ax.plot(xs_hm, ys_hm, color=C_REAL_SAT, lw=1.5, marker="s", ms=4, ls="--",
                    alpha=0.75, label="Real harmonic mean")

    if has_sim and has_real:
        # 凡例の補足テキスト
        sim_label = sim["log_path"].name
        real_label = real["central"].name
        ax.text(0.01, 0.03, f"Sim: {sim_label}\nReal: {real_label}",
                transform=ax.transAxes, fontsize=7, color="gray", va="bottom")

    ax.set_xlabel("Global Round", fontsize=11)
    ax.set_ylabel("Satisfaction (%)", fontsize=11)
    ax.set_ylim(0, 110)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    all_rnds = sorted(set(sim_rnds) | set(real_rnds))
    if all_rnds:
        ax.set_xlim(all_rnds[0] - 0.5, all_rnds[-1] + 0.5)
        span = all_rnds[-1] - all_rnds[0]
        ax.xaxis.set_major_locator(ticker.MultipleLocator(max(1, span // 10)))

    out = out_dir / "combined_satisfaction.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# エントリポイント
# ══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sim vs Real 比較グラフ生成 (separate + combined)"
    )
    ap.add_argument("--sim-log",      type=str, default=None,
                    help="Sim_HFL JSONL ログパス（省略時: 最新を自動選択）")
    ap.add_argument("--real-central", type=str, default=None,
                    help="central_server_*.log パス（省略時: 最新を自動選択）")
    ap.add_argument("--out-dir", type=str, default="analysis_output/comparison",
                    help="出力先ディレクトリ（既定: analysis_output/comparison）")
    ap.add_argument("--no-sim",  action="store_true", help="シミュレーション単体グラフをスキップ")
    ap.add_argument("--no-real", action="store_true", help="実機単体グラフをスキップ")
    ap.add_argument("--no-combined", action="store_true", help="重ね合わせグラフをスキップ")
    args = ap.parse_args()

    # 出力先
    out_dir = (SERVERSIDE_ROOT / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── シミュレーションデータ ──
    sim_log = Path(args.sim_log) if args.sim_log else _pick_sim_log()
    if sim_log is None or not sim_log.exists():
        print("WARN: シミュレーションログが見つかりません。--sim-log で指定してください。",
              file=sys.stderr)
        sim_data = None
    else:
        print(f"[SIM]  {sim_log}")
        sim_data = load_sim_data(sim_log)
        print(f"       rounds={len(sim_data['rounds'])}  "
              f"val_acc_range={min(sim_data['val_acc']):.1f}%–{max(sim_data['val_acc']):.1f}%")

    # ── 実機データ ──
    real_central = Path(args.real_central) if args.real_central else _find_latest_central()
    if real_central is None or not real_central.exists():
        print("WARN: 実機中央ログが見つかりません。--real-central で指定してください。",
              file=sys.stderr)
        real_data = None
    else:
        print(f"[REAL] {real_central}")
        real_data = load_real_data(real_central)
        print(f"       rounds={len(real_data['rounds'])}  "
              f"terminals={len(real_data['time_per_terminal'])}")

    # ── グラフ生成 ──
    generated: List[Path] = []

    if sim_data and not args.no_sim and sim_data["rounds"]:
        p = plot_separate_sim(sim_data, out_dir)
        generated.append(p)
        print(f"[OUT]  {p}")

    if real_data and not args.no_real:
        p = plot_separate_real(real_data, out_dir)
        generated.append(p)
        print(f"[OUT]  {p}")

    if sim_data and real_data and not args.no_combined:
        p = plot_combined_satisfaction(sim_data, real_data, out_dir)
        generated.append(p)
        print(f"[OUT]  {p}")

    if not generated:
        print("生成されたグラフがありません。データを確認してください。", file=sys.stderr)
        return 1

    print(f"\n出力先: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
