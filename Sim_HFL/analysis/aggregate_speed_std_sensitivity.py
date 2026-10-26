#!/usr/bin/env python3
"""
aggregate_speed_std_sensitivity.py
==================================
speed_std 感度分析の結果を 4 条件 × 5試行 から集計し、
Markdown 表で標準出力に書き出す。

使い方:
  python3 analysis/aggregate_speed_std_sensitivity.py \
      --conditions 0.3 0.5 1.0 2.0 \
      --logs-dir results/logs \
      --since-min 30   # 直近30分以内のログを対象

各 yaml 名から speed_std を抽出するのではなく、ログ内
runner_info の speed_mean/speed_std から取得する。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Optional


def _parse_runner_info(msg: str) -> dict:
    """runner_info の msg 文字列から K=V を抽出"""
    out = {}
    # e.g. "speed_mean=1.0  speed_std=0.3"
    for m in re.finditer(r"(\w+)=([0-9.\-eE]+)", msg):
        try:
            out[m.group(1)] = float(m.group(2))
        except Exception:
            pass
    return out


def _scan_log(path: Path) -> Optional[dict]:
    """1 ログファイルから集計指標を抽出。async_summary を最終出力として使う。"""
    speed_std: Optional[float] = None
    final_global_round: Optional[int] = None
    push_count_cv: Optional[float] = None
    staleness_mean: Optional[float] = None
    staleness_p95: Optional[float] = None
    staleness_max: Optional[int] = None
    n_pushes: Optional[int] = None
    n_drops: int = 0

    # 最終ラウンドの val_acc (aggregation_complete の末尾)
    last_val_acc: Optional[float] = None
    last_val_loss: Optional[float] = None

    # 全 satisfaction_summary の HM 平均
    hm_list: list[float] = []
    n_switched_total: int = 0
    capacity_overflow_total: int = 0
    last_hm: Optional[float] = None

    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                ev = evt.get("event")
                if ev == "runner_info":
                    kv = _parse_runner_info(evt.get("msg", ""))
                    if "speed_std" in kv:
                        speed_std = kv["speed_std"]
                elif ev == "async_summary":
                    final_global_round = evt.get("final_global_round")
                    push_count_cv = evt.get("push_count_cv")
                    staleness_mean = evt.get("staleness_mean")
                    staleness_p95 = evt.get("staleness_p95")
                    staleness_max = evt.get("staleness_max")
                    n_pushes = evt.get("n_pushes")
                elif ev == "terminal_stale_drop":
                    n_drops += 1
                elif ev == "aggregation_complete":
                    last_val_acc = evt.get("avg_val_acc", last_val_acc)
                    last_val_loss = evt.get("avg_val_loss", last_val_loss)
                elif ev == "satisfaction_summary":
                    hm = evt.get("harmonic_mean_satisfaction")
                    if hm is not None:
                        hm_list.append(hm)
                        last_hm = hm
                    n_switched_total += int(evt.get("n_switched", 0) or 0)
                    capacity_overflow_total += int(evt.get("capacity_overflow_count", 0) or 0)
    except Exception:
        return None

    # async_summary が出てない (＝途中) は除外
    if final_global_round is None:
        return None

    # 平均 HM は全ステップの平均、末尾 HM は最終ステップ
    avg_hm = (sum(hm_list) / len(hm_list)) if hm_list else 0.0

    drop_rate = (n_drops / max((n_pushes or 0) + n_drops, 1)) if (n_drops or n_pushes) else 0.0

    return {
        "path": str(path),
        "speed_std": speed_std,
        "final_global_round": final_global_round,
        "push_count_cv": push_count_cv,
        "staleness_mean": staleness_mean,
        "staleness_p95": staleness_p95,
        "staleness_max": staleness_max,
        "n_pushes": n_pushes,
        "n_drops": n_drops,
        "drop_rate": drop_rate,
        "val_acc": last_val_acc or 0.0,
        "val_loss": last_val_loss or 0.0,
        "hm_avg": avg_hm,
        "hm_last": last_hm or 0.0,
        "n_switched_total": n_switched_total,
        "capacity_overflow_total": capacity_overflow_total,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--logs-dir", default="results/logs")
    p.add_argument("--conditions", nargs="+", type=float,
                   default=[0.3, 0.5, 1.0, 2.0],
                   help="speed_std の値で絞り込む")
    p.add_argument("--since-min", type=int, default=120,
                   help="直近 N 分以内のログのみ対象 (default: 120)")
    p.add_argument("--output", default=None,
                   help="Markdown を書き出すファイル (省略時 stdout)")
    args = p.parse_args()

    logs_dir = Path(args.logs_dir)
    cutoff = time.time() - args.since_min * 60

    # 全ログ走査
    all_records: list[dict] = []
    for path in sorted(logs_dir.glob("rounds_*.jsonl")):
        if path.stat().st_mtime < cutoff:
            continue
        rec = _scan_log(path)
        if rec is None:
            continue
        all_records.append(rec)

    # speed_std でグルーピング
    by_std: dict[float, list[dict]] = {}
    for rec in all_records:
        s = rec["speed_std"]
        if s is None:
            continue
        # 引数で指定された値とマッチするものだけ採用
        for target in args.conditions:
            if abs(s - target) < 1e-6:
                by_std.setdefault(target, []).append(rec)
                break

    # Markdown 出力
    lines = []
    lines.append("# Speed_std 感度分析結果\n")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"対象ログ: 直近 {args.since_min} 分、{sum(len(v) for v in by_std.values())} 試行\n")
    lines.append("")
    lines.append("## サマリー表 (mean ± std)")
    lines.append("")
    lines.append("| speed_std | n | val_acc | HM_avg | HM_last | staleness_mean | staleness_p95 | staleness_max | drop_rate | push_cv | n_switched | overflow |")
    lines.append("|-----------|---|---------|--------|---------|----------------|---------------|---------------|-----------|---------|------------|----------|")

    def _ms(vals: list[float], fmt: str = ".3f") -> str:
        if not vals:
            return "—"
        m = statistics.mean(vals)
        if len(vals) >= 2:
            s = statistics.stdev(vals)
            return f"{m:{fmt}} ± {s:{fmt}}"
        return f"{m:{fmt}}"

    for target in sorted(by_std.keys()):
        recs = by_std[target]
        n = len(recs)
        val_acc_s = _ms([r["val_acc"] for r in recs], ".4f")
        hm_avg_s  = _ms([r["hm_avg"] for r in recs], ".4f")
        hm_last_s = _ms([r["hm_last"] for r in recs], ".4f")
        sm_s  = _ms([r["staleness_mean"] for r in recs if r["staleness_mean"] is not None], ".3f")
        sp_s  = _ms([float(r["staleness_p95"]) for r in recs if r["staleness_p95"] is not None], ".1f")
        smx_s = _ms([float(r["staleness_max"]) for r in recs if r["staleness_max"] is not None], ".1f")
        dr_s  = _ms([r["drop_rate"] for r in recs], ".3f")
        pcv_s = _ms([r["push_count_cv"] for r in recs if r["push_count_cv"] is not None], ".3f")
        nsw_s = _ms([float(r["n_switched_total"]) for r in recs], ".1f")
        ovf_s = _ms([float(r["capacity_overflow_total"]) for r in recs], ".1f")
        lines.append(f"| {target} | {n} | {val_acc_s} | {hm_avg_s} | {hm_last_s} | {sm_s} | {sp_s} | {smx_s} | {dr_s} | {pcv_s} | {nsw_s} | {ovf_s} |")

    lines.append("")
    lines.append("## 試行ごとの詳細")
    lines.append("")
    lines.append("| speed_std | run | val_acc | HM_avg | staleness_mean | drop_rate | log |")
    lines.append("|-----------|-----|---------|--------|----------------|-----------|-----|")
    for target in sorted(by_std.keys()):
        for i, r in enumerate(by_std[target], 1):
            sm = r['staleness_mean'] if r['staleness_mean'] is not None else 0.0
            lines.append(
                f"| {target} | {i} | {r['val_acc']:.4f} | {r['hm_avg']:.4f} | "
                f"{sm:.3f} | {r['drop_rate']:.3f} | `{Path(r['path']).name}` |"
            )
    lines.append("")

    out = "\n".join(lines)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"Written: {args.output}", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
