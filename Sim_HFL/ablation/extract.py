"""1試行の JSONL ログから学習指標を取り出す。"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

TAIL = 10  # 満足度・val_acc は最後の TAIL ラウンドの平均（収束後の値として）


def extract(jsonl: Path) -> dict:
    agg, sat = [], []
    async_summary = None
    mode = None
    for line in open(jsonl, encoding="utf-8"):
        d = json.loads(line)
        ev = d.get("event")
        if ev == "aggregation_complete":
            agg.append((d["round_id"], d["avg_val_acc"]))
        elif ev == "satisfaction_summary":
            sat.append((d["round_id"], d["avg_satisfaction"], d.get("harmonic_mean_satisfaction")))
        elif ev == "async_summary":
            async_summary = d
        elif ev == "runner_info" and "mode=" in d.get("msg", ""):
            mode = "async" if "mode=async" in d["msg"] else mode
    if not agg:
        raise ValueError(f"aggregation_complete が無い: {jsonl}")
    agg.sort()
    sat.sort()
    out = {
        "mode": mode or "sync",
        "global_rounds": agg[-1][0],
        "val_acc_tail": statistics.mean(v for _, v in agg[-TAIL:]),
        "sat_mean_tail": statistics.mean(s for _, s, _ in sat[-TAIL:]) if sat else None,
        "sat_hm_tail": (statistics.mean(h for _, _, h in sat[-TAIL:] if h is not None)
                        if sat and any(h is not None for _, _, h in sat[-TAIL:]) else None),
        "staleness_mean": None, "staleness_p95": None, "staleness_max": None,
    }
    if async_summary:
        out["global_rounds"] = async_summary["final_global_round"]
        out["staleness_mean"] = async_summary["staleness_mean"]
        out["staleness_p95"] = async_summary["staleness_p95"]
        out["staleness_max"] = async_summary["staleness_max"]
    return out
