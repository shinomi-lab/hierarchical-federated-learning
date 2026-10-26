#!/usr/bin/env python3
"""中央モデル（centralized-mode）と HFL の「精度に到達するまでのラウンド・時間」を並べる事後分析（実験中のコードには入れない）。

実行: Serverside_HFL/.venv/bin/python3 scripts/compare_central_vs_hfl.py \
        --db received_edges/training_metrics.db --central-meta state/central_model.meta.json [--run-id R ...] [--data-bytes N] [--target 0.78] [--csv out.csv]

- HFL: run ごとに、サイクル番号ごとの端末平均の精度（最終エポックの検証精度）が目標に最初に届いたサイクル・ラウンド・経過時間。
  経過時間は run の最初のサイクル行の時刻から（その行のサイクル自体の学習時間は含まない）。
- 中央: 学習の壁時計時間（メタの train_seconds）＋データ収集の見積り。見積りは「教師データのサイズを端末数で割った量 ÷ 各端末の実測スループット」の
  最も遅い端末（並列に送る前提・圧縮なし）で、実機で測った量ではない。tp_unit="Mbps" の行だけを使い、旧単位（MB/s を Mbps としていた）は使わない。
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional


def _load_cycle_rows(db) -> Dict[str, List[dict]]:
    """run_id → サイクルの行（学習した行）。要約行（baseline_session など）と精度の無い行は除く。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT run_id, terminal_id, cycle_number, round, accuracy, timestamp_ms, tp_measured_mbps, raw_json "
            "FROM metrics WHERE run_id IS NOT NULL AND cycle_number IS NOT NULL AND accuracy IS NOT NULL "
            "ORDER BY timestamp_ms, id").fetchall()
    finally:
        conn.close()
    out: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        raw = {}
        if r["raw_json"]:
            try:
                raw = json.loads(r["raw_json"])
            except json.JSONDecodeError:
                pass
        if raw.get("row_kind") == "baseline_session":
            continue
        d = dict(r)
        d["tp_unit"] = raw.get("tp_unit")
        out[r["run_id"]].append(d)
    return out


def _collect_estimate(rows: List[dict], data_bytes: Optional[int]):
    """(収集時間の見積り秒 or None, 理由メモ)。"""
    if not data_bytes:
        return None, "data_bytes が無い（--data-bytes か、メタの csv のサイズが要る）"
    terms = sorted({r["terminal_id"] for r in rows if r["terminal_id"]})
    if not terms:
        return None, "端末 ID が無い"
    per_term_bytes = data_bytes / len(terms)
    worst = 0.0
    for t in terms:
        tps = [r["tp_measured_mbps"] for r in rows
               if r["terminal_id"] == t and r["tp_unit"] == "Mbps" and r["tp_measured_mbps"] and r["tp_measured_mbps"] > 0]
        if not tps:
            return None, f"{t} に tp_unit=Mbps の実測スループットが無い（旧単位の行は使わない）"
        worst = max(worst, per_term_bytes * 8 / (mean(tps) * 1e6))
    return worst, ""


def compare(db, central_meta: dict, run_ids: Optional[List[str]], data_bytes: Optional[int] = None,
            target: Optional[float] = None) -> List[dict]:
    target = float(central_meta["val_accuracy"]) if target is None else float(target)
    by_run = _load_cycle_rows(db)
    out = []
    for run in (run_ids if run_ids else sorted(by_run)):
        rows = by_run.get(run, [])
        if not rows:
            continue
        start = min(r["timestamp_ms"] for r in rows if r["timestamp_ms"] is not None)
        cycles = sorted({r["cycle_number"] for r in rows})
        reached = None
        for c in cycles:
            crs = [r for r in rows if r["cycle_number"] == c]
            if mean(r["accuracy"] for r in crs) >= target:
                reached = c
                break
        final_rows = [r for r in rows if r["cycle_number"] == cycles[-1]]
        res = {
            "run_id": run,
            "n_terminals": len({r["terminal_id"] for r in rows}),
            "n_cycles": len(cycles),
            "target_accuracy": target,
            "hfl_reached_cycle": reached,
            "hfl_reached_round": None,
            "hfl_elapsed_s": None,
            "hfl_final_accuracy": mean(r["accuracy"] for r in final_rows),
            "central_val_accuracy": float(central_meta["val_accuracy"]),
            "central_train_s": float(central_meta["train_seconds"]),
        }
        if reached is not None:
            crs = [r for r in rows if r["cycle_number"] == reached]
            rounds = [r["round"] for r in crs if r["round"] is not None]
            res["hfl_reached_round"] = min(rounds) if rounds else None
            res["hfl_elapsed_s"] = (max(r["timestamp_ms"] for r in crs) - start) / 1000.0
        collect, note = _collect_estimate(rows, data_bytes)
        res["central_collect_est_s"] = collect
        res["central_total_est_s"] = None if collect is None else res["central_train_s"] + collect
        res["collect_note"] = note
        out.append(res)
    return out


def _fmt(v, nd=2):
    return "—" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def render_markdown(rows: List[dict], central_meta: dict) -> str:
    lines = [
        "| run | 端末数 | 目標精度 | HFL 到達サイクル | HFL 到達ラウンド | HFL 経過(s) | HFL 最終精度 | 中央 検証精度 | 中央 学習(s) | 中央 収集(s,推定) | 中央 合計(s,推定) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['run_id']} | {r['n_terminals']} | {_fmt(r['target_accuracy'], 3)} | {_fmt(r['hfl_reached_cycle'])} | "
            f"{_fmt(r['hfl_reached_round'])} | {_fmt(r['hfl_elapsed_s'], 1)} | {_fmt(r['hfl_final_accuracy'], 3)} | "
            f"{_fmt(r['central_val_accuracy'], 3)} | {_fmt(r['central_train_s'], 1)} | {_fmt(r['central_collect_est_s'], 1)} | "
            f"{_fmt(r['central_total_est_s'], 1)} |")
    notes = [
        "",
        f"中央モデル: 行数 {central_meta.get('rows')}、エポック {central_meta.get('epochs')}、seed {central_meta.get('seed')}、SHA256 {str(central_meta.get('model_sha256'))[:12]}…",
        "- 「推定」の列は実機で測っていない。中央の収集時間 = 教師データのサイズ ÷ 端末数 ÷ 各端末の実測スループット（tp_unit=Mbps の行のみ）の最大値。圧縮なし・並列送信の前提。",
        "- 検証精度の集合が違う: 中央は全データの 10%（ファイル順）、HFL は各端末の担当分の検証（最終エポックの値）を端末平均したもの。精度は上限（約 81%）に近く、差は小さい。",
        "- HFL の経過時間は run の最初のサイクル行の時刻から測る（その行のサイクルの学習時間は含まない）。",
    ]
    for r in rows:
        if r.get("collect_note"):
            notes.append(f"- {r['run_id']}: 収集時間は出せない（{r['collect_note']}）")
    return "\n".join(lines + notes) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="received_edges/training_metrics.db")
    ap.add_argument("--central-meta", default="state/central_model.meta.json")
    ap.add_argument("--run-id", action="append", help="省略すると、サイクルの行がある全 run")
    ap.add_argument("--data-bytes", type=int, help="教師データのサイズ(バイト)。省略するとメタの csv のファイルサイズ")
    ap.add_argument("--target", type=float, help="目標精度。省略すると中央モデルの検証精度")
    ap.add_argument("--csv", help="表を CSV でも書き出す")
    a = ap.parse_args(argv)
    meta = json.loads(Path(a.central_meta).read_text(encoding="utf-8"))
    data_bytes = a.data_bytes
    if data_bytes is None and meta.get("csv") and Path(meta["csv"]).is_file():
        data_bytes = Path(meta["csv"]).stat().st_size
    rows = compare(a.db, meta, a.run_id, data_bytes=data_bytes, target=a.target)
    if not rows:
        print("サイクルの行がある run が見つかりません", file=sys.stderr)
        return 1
    print(render_markdown(rows, meta))
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
