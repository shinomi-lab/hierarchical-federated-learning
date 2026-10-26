"""全条件×多数 seed の完了ステップ数 (Async / Sync) を CSV に出す。学習は走らせない。"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
from pathlib import Path

from . import conditions as C
from . import timemodel as T


def run(out_dir: Path, seeds: int, cycles: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for c in C.build_conditions():
        cfg = c["cfg"]
        n = cfg["federation"]["num_terminals"]
        mean = cfg["async"]["terminal_speed_mean"]
        std = cfg["async"]["terminal_speed_std"]
        for rep in range(seeds):
            a, s = T.simulate(1000 + rep, n, cycles, mean, std)
            rows.append({"condition": c["name"], "axis": c["axis"] or "", "level": c["level"] or "",
                         "seed": 1000 + rep, "speed_std": std, "cycles": cycles,
                         "async_steps": a, "sync_steps": s})
    raw = out_dir / "time_raw.csv"
    with open(raw, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    summ = out_dir / "time_summary.csv"
    with open(summ, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["condition", "speed_std", "n", "async_mean", "async_ci95", "sync_mean", "sync_ci95",
                    "ratio_sync_over_async", "wait_steps_mean", "wait_ci95"])
        for name in dict.fromkeys(r["condition"] for r in rows):
            rs = [r for r in rows if r["condition"] == name]
            a = [r["async_steps"] for r in rs]
            s = [r["sync_steps"] for r in rs]
            d = [x - y for x, y in zip(s, a)]
            ci = lambda v: 1.96 * statistics.stdev(v) / math.sqrt(len(v)) if len(v) > 1 else 0.0
            w.writerow([name, rs[0]["speed_std"], len(rs),
                        f"{statistics.mean(a):.3f}", f"{ci(a):.3f}",
                        f"{statistics.mean(s):.3f}", f"{ci(s):.3f}",
                        f"{statistics.mean(s)/statistics.mean(a):.3f}",
                        f"{statistics.mean(d):.3f}", f"{ci(d):.3f}"])
    return summ


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="results/ablation_time")
    p.add_argument("--seeds", type=int, default=1000)
    p.add_argument("--cycles", type=int, default=30)
    a = p.parse_args()
    print(run(Path(a.out), a.seeds, a.cycles))
