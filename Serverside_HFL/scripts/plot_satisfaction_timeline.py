#!/usr/bin/env python3
"""満足度の時系列（Before → サイクルごと → After の各値）を描く（SP3d）。

1 つの run_id について、端末ごとに次の順で点を並べる:
  x=0 Before（satisfaction_before）
  x=1..N サイクルごとの satisfaction_cycle（学習のセッション）
        または ベースライン計測の各回（ベースラインの要約行。5 回 = 1 サイクルの位置）
  x=N+1.. After の各回（satisfaction_after_samples。失敗した回は点を打たないが位置は保つ。
        配列の無い旧データは satisfaction_after を 1 点として扱う）

例: Serverside_HFL/.venv/bin/python3 scripts/plot_satisfaction_timeline.py \\
      --db received_edges/training_metrics.db --run-id <run_id> [--terminal terminal-01] --out fig.png
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional

BASELINE_MEASUREMENTS_PER_CYCLE = 5


def _num(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN は欠損


def timeline_points(rows: List[dict]) -> List[dict]:
    """1 つの run_id・端末の metrics 行（raw_json 展開済み）から時系列の点を作る（純関数）。"""
    pts: List[dict] = []
    before = next((_num(r.get("satisfaction_before")) for r in rows if _num(r.get("satisfaction_before")) is not None), None)
    if before is not None:
        pts.append({"x": 0, "kind": "before", "value": before})

    cycle_rows = sorted((r for r in rows if r.get("cycle_number") is not None), key=lambda r: int(r["cycle_number"]))
    baseline = next((r for r in rows if r.get("row_kind") == "baseline_session"), None)
    end = 0
    if cycle_rows:
        for r in cycle_rows:
            c = int(r["cycle_number"])
            end = max(end, c)
            v = _num(r.get("satisfaction_cycle"))
            if v is not None:
                pts.append({"x": c, "kind": "cycle", "cycle": c, "value": v})
    elif baseline is not None:
        samples = baseline.get("satisfaction_baseline_samples") or []
        for i, v in enumerate(samples, start=1):
            if _num(v) is not None:
                pts.append({"x": i / BASELINE_MEASUREMENTS_PER_CYCLE, "kind": "baseline", "measurement": i, "value": _num(v)})
        end = int(baseline.get("num_cycles") or -(-len(samples) // BASELINE_MEASUREMENTS_PER_CYCLE))

    # After は最終サイクルの行（ベースラインは要約行）に補完されている
    after_row = cycle_rows[-1] if cycle_rows else baseline
    if after_row is not None:
        samples = after_row.get("satisfaction_after_samples")
        if samples is None and _num(after_row.get("satisfaction_after")) is not None:
            samples = [after_row["satisfaction_after"]]   # 旧データ（1 回計測）
        for i, v in enumerate(samples or [], start=1):
            if _num(v) is not None:
                pts.append({"x": end + i, "kind": "after", "sample": i, "value": _num(v)})
    return pts


def load_rows(db_path, run_id: str, terminal: Optional[str] = None) -> Dict[str, List[dict]]:
    """metrics から run_id の行を読み、raw_json を展開して端末ごとに返す（cycle_number 順）。"""
    q = "SELECT terminal_id, raw_json FROM metrics WHERE run_id = ?"
    args: list = [run_id]
    if terminal:
        q += " AND terminal_id = ?"
        args.append(terminal)
    q += " ORDER BY cycle_number"
    out: Dict[str, List[dict]] = {}
    with sqlite3.connect(str(db_path)) as conn:
        for tid, raw in conn.execute(q, args):
            try:
                row = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                row = {}
            out.setdefault(tid or row.get("terminal_id") or "?", []).append(row)
    return out


def plot(per_terminal: Dict[str, List[dict]], out: Path, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4.5))
    for tid, rows in sorted(per_terminal.items()):
        pts = timeline_points(rows)
        line = [p for p in pts if p["kind"] in ("before", "cycle", "baseline")]
        h = ax.plot([p["x"] for p in line], [p["value"] for p in line], marker="o", markersize=3, label=tid)[0]
        color = h.get_color()
        before = [p for p in pts if p["kind"] == "before"]
        if before:
            ax.scatter([before[0]["x"]], [before[0]["value"]], marker="s", color=color, zorder=3)
        after = [p for p in pts if p["kind"] == "after"]
        if after:
            ax.scatter([p["x"] for p in after], [p["value"] for p in after], marker="^", color=color, zorder=3)
            mean = sum(p["value"] for p in after) / len(after)
            ax.hlines(mean, after[0]["x"] - 0.3, after[-1]["x"] + 0.3, colors=color, linestyles="dashed")
    ax.set_xlabel("Before (0) → cycles → After samples")
    ax.set_ylabel("satisfaction")
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out), dpi=150)
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--terminal")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    per_terminal = load_rows(a.db, a.run_id, a.terminal)
    if not per_terminal:
        print(f"run_id={a.run_id} の行がありません", file=sys.stderr)
        return 1
    plot(per_terminal, Path(a.out), f"run_id={a.run_id}")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
