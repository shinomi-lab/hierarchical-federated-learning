#!/usr/bin/env python3
"""教師 CSV（tpNeed, rttNeed, appNum, label）の要求TP・要求RTT の平均・母標準偏差を求め、推論の正規化に使う norm_stats.json を書く。
端末 CsvLoader.loadFlexible と同じ計算（std<=1e-8 なら 1）。

実行: Serverside_HFL/.venv/bin/python3 Serverside_HFL/tools/make_norm_stats.py HFL_terminals/app/src/main/assets/training_data.csv \
        Serverside_HFL/state/norm_stats.json HFL_terminals/app/src/test/resources/golden/norm_stats.json
"""
import csv
import json
import math
import sys
from pathlib import Path


def compute(csv_path) -> dict:
    tps, rtts = [], []
    for row in csv.reader(open(csv_path, encoding="utf-8")):
        if not row or row[0].startswith("#"):
            continue
        try:
            tp, rtt = float(row[0]), float(row[1])
        except ValueError:
            continue
        tps.append(tp); rtts.append(rtt)
    def stats(v):
        m = sum(v) / len(v)
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
        return m, (sd if sd > 1e-8 else 1.0)
    tm, ts = stats(tps); rm, rs = stats(rtts)
    return {"tp_mean": tm, "tp_std": ts, "rtt_mean": rm, "rtt_std": rs, "rows": len(tps), "source": str(csv_path)}


def main(argv):
    s = compute(argv[0])
    for out in argv[1:]:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(s, indent=2) + "\n")
    print(json.dumps(s))


if __name__ == "__main__":
    main(sys.argv[1:])
