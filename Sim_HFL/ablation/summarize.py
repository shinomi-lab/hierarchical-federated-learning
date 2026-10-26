"""learning_raw.csv から要約 Markdown を作る（崩壊の数・Fisher 検定・条件ごとの平均）。

崩壊 = 最後の 10 ラウンドの満足度(平均)が COLLAPSE_SAT 未満（全端末が同じ AP を選ぶ状態、満足度 約 0.5075）。
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics as st
from collections import defaultdict
from pathlib import Path

COLLAPSE_SAT = 0.55
ORDER = ["base", "speed_0", "speed_0.5", "speed_1.0", "data_dir0.5", "data_dir0.1", "ap_ch1", "ap_ch4", "apmu_mu1", "apmu_mu4"]


def fisher_two_sided(a: int, b: int, c: int, d: int) -> float:
    """[[a,b],[c,d]] の Fisher 正確確率（両側）。"""
    n = a + b + c + d
    r1, c1 = a + b, a + c

    def p(x: int) -> float:
        return math.comb(r1, x) * math.comb(n - r1, c1 - x) / math.comb(n, c1)

    p0 = p(a)
    lo, hi = max(0, c1 - (n - r1)), min(r1, c1)
    return sum(p(x) for x in range(lo, hi + 1) if p(x) <= p0 + 1e-12)


def load(paths: list[Path]) -> list[dict]:
    rows = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            rows += [r for r in csv.DictReader(f) if r["status"] == "ok"]
    return rows


def _vals(rs: list[dict], key: str) -> list[float]:
    return [float(r[key]) for r in rs if r.get(key) not in ("", None)]


def _ms(v: list[float], nd: int = 4) -> str:
    if not v:
        return "—"
    sd = st.stdev(v) if len(v) > 1 else 0.0
    return f"{st.mean(v):.{nd}f}±{sd:.{nd}f}"


def summarize(rows: list[dict]) -> str:
    by = defaultdict(list)
    for r in rows:
        by[(r["condition"], r["mode"])].append(r)
    conds = [c for c in ORDER if any(k[0] == c for k in by)] + sorted({k[0] for k in by} - set(ORDER))
    out = ["## 条件別（最後の 10 ラウンドの平均。平均±標準偏差）", "",
           "| 条件 | 方式 | n | 満足度(平均) | 満足度(調和平均) | staleness 平均 | staleness 最大 | 崩壊した試行 |",
           "|---|---|---|---|---|---|---|---|"]
    for c in conds:
        for m in ("async", "sync"):
            rs = by.get((c, m), [])
            if not rs:
                continue
            sat = _vals(rs, "sat_mean_tail")
            col = sum(1 for s in sat if s < COLLAPSE_SAT)
            # 崩壊の分母は、満足度が空でない（有効な）試行の数。空の行を分母に含めると崩壊率が薄まる
            out.append(f"| {c} | {m} | {len(rs)} | {_ms(sat)} | {_ms(_vals(rs, 'sat_hm_tail'))} | "
                       f"{_ms(_vals(rs, 'staleness_mean'), 3)} | {_ms(_vals(rs, 'staleness_max'), 2)} | {col}/{len(sat)} |")
    out += ["", f"## 崩壊（満足度 < {COLLAPSE_SAT}）の数: Async と Sync の比較（Fisher の正確確率検定、両側）", "",
            "| 条件 | Async 崩壊/n | Sync 崩壊/n | p |", "|---|---|---|---|"]
    pooled = [0, 0, 0, 0]
    for c in conds:
        a, s = by.get((c, "async"), []), by.get((c, "sync"), [])
        if not a or not s or not c.startswith("data_dir"):
            continue
        sa, ss = _vals(a, "sat_mean_tail"), _vals(s, "sat_mean_tail")   # 空の行は除く
        if not sa or not ss:
            continue
        ca = sum(1 for x in sa if x < COLLAPSE_SAT)
        cs = sum(1 for x in ss if x < COLLAPSE_SAT)
        out.append(f"| {c} | {ca}/{len(sa)} | {cs}/{len(ss)} | {fisher_two_sided(ca, len(sa) - ca, cs, len(ss) - cs):.3f} |")
        pooled[0] += ca; pooled[1] += len(sa) - ca; pooled[2] += cs; pooled[3] += len(ss) - cs
    if sum(pooled):
        out.append(f"| 合算 | {pooled[0]}/{pooled[0] + pooled[1]} | {pooled[2]}/{pooled[2] + pooled[3]} | "
                   f"{fisher_two_sided(*pooled):.3f} |")
    out += ["", "満足度で見る。val_acc は非 IID で検証データも偏るため、条件間の比較に使わない（`sim_ablation_20261021.md` §0）。"]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    md = summarize(load([Path(p) for p in a.csv]))
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
        print(a.out)
    else:
        print(md)
