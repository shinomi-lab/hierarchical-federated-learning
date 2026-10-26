#!/usr/bin/env python3
"""
docker/analyze_ch4_rerun.py — 第4章 再計測の集計（試行単位）。
  python3 docker/analyze_ch4_rerun.py experiment_results/docker_rerun_20261015/trials.csv [--out report.md]

有効な試行だけを使い、N ごとに Async と Sync の完了時間を Welch の t 検定（両側、t 分布）で比べ、
主比較（N=4,6,8）に Holm 補正をかける。標準ライブラリのみ（p は正則化不完全ベータ関数で計算）。
"""
from __future__ import annotations

import argparse
import collections
import csv
import math
import statistics


def _betacf(a, b, x):
    MAXIT, EPS, FPMIN = 500, 3e-15, 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > FPMIN else FPMIN)
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d; d = d if abs(d) > FPMIN else FPMIN
        c = 1 + aa / c; c = c if abs(c) > FPMIN else FPMIN
        d = 1 / d; h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d; d = d if abs(d) > FPMIN else FPMIN
        c = 1 + aa / c; c = c if abs(c) > FPMIN else FPMIN
        d = 1 / d; de = d * c; h *= de
        if abs(de - 1) < EPS:
            break
    return h


def t_p2(t, df):
    if math.isinf(t):
        return 0.0
    x = df / (df + t * t)
    a, b = df / 2, 0.5
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    return bt * _betacf(a, b, x) / a if x < (a + 1) / (a + b + 2) else 1 - bt * _betacf(b, a, 1 - x) / b


def welch(a, b):
    ma, mb = statistics.mean(a), statistics.mean(b)
    va, vb = statistics.variance(a), statistics.variance(b)
    se2 = va / len(a) + vb / len(b)
    if se2 == 0:
        return float("nan"), float("nan"), float("nan")
    t = (ma - mb) / math.sqrt(se2)
    df = se2 ** 2 / ((va / len(a)) ** 2 / (len(a) - 1) + (vb / len(b)) ** 2 / (len(b) - 1))
    return t, df, t_p2(t, df)


def hedges_g(a, b):
    na, nb = len(a), len(b)
    sp = math.sqrt(((na - 1) * statistics.variance(a) + (nb - 1) * statistics.variance(b)) / (na + nb - 2))
    if sp == 0:
        return float("nan")
    return (statistics.mean(a) - statistics.mean(b)) / sp * (1 - 3 / (4 * (na + nb) - 9))


def holm(ps):
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adj, run = [0.0] * len(ps), 0.0
    for k, i in enumerate(order):
        run = max(run, min(1.0, (len(ps) - k) * ps[i]))
        adj[i] = run
    return adj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trials_csv")
    ap.add_argument("--primary", default="4,6,8")
    ap.add_argument("--out")
    args = ap.parse_args()
    rows = list(csv.DictReader(open(args.trials_csv)))
    valid = [r for r in rows if r["valid"] == "1"]
    invalid = collections.Counter((r["mode"], r["n"], r["invalid_reason"]) for r in rows if r["valid"] != "1")
    by = collections.defaultdict(list)
    for r in valid:
        by[(r["mode"], int(r["n"]))].append(float(r["elapsed_s"]))
    primary = [int(x) for x in args.primary.split(",")]
    ns = sorted({n for _, n in by})
    commits = sorted({r["git_commit"] for r in rows})

    out = [f"# 第4章 Docker 再計測 — 集計（{args.trials_csv}）", "",
           f"- 試行: 全 {len(rows)}、有効 {len(valid)}、無効 {len(rows) - len(valid)}。コミット: {', '.join(commits)}",
           "- 検定: 試行単位、Welch の t 検定（両側、t 分布）。Holm は主比較 N=" + args.primary + " の範囲。", "",
           "| N | Async 平均±SD (n) | Sync 平均±SD (n) | Sync/Async | t (df) | p | Holm p | Hedges g |",
           "|---|---|---|---|---|---|---|---|"]
    res = {}
    for n in ns:
        a, s = by.get(("async", n), []), by.get(("sync", n), [])
        if len(a) >= 2 and len(s) >= 2:
            res[n] = (a, s, *welch(a, s), hedges_g(a, s))
    prim = [n for n in primary if n in res]
    adj = dict(zip(prim, holm([res[n][4] for n in prim])))
    for n in ns:
        a, s = by.get(("async", n), []), by.get(("sync", n), [])
        fmt = lambda v: f"{statistics.mean(v):.1f}±{statistics.stdev(v):.1f} ({len(v)})" if len(v) >= 2 else (f"{v[0]:.1f} (1)" if v else "—")
        if n in res:
            _, _, t, df, p, g = res[n]
            ratio = statistics.mean(s) / statistics.mean(a)
            out.append(f"| {n} | {fmt(a)} | {fmt(s)} | {ratio:.2f} | {t:.2f} ({df:.1f}) | {p:.2g} | "
                       f"{adj[n]:.2g} | {g:.2f} |" if n in adj else
                       f"| {n} | {fmt(a)} | {fmt(s)} | {ratio:.2f} | {t:.2f} ({df:.1f}) | {p:.2g} | — | {g:.2f} |")
        else:
            out.append(f"| {n} | {fmt(a)} | {fmt(s)} | — | — | — | — | — |")
    out += ["", "## 無効な試行", ""]
    out += [f"- {m} N={n}: {why} × {c}" for (m, n, why), c in sorted(invalid.items())] or ["- なし"]
    text = "\n".join(out) + "\n"
    print(text)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)


if __name__ == "__main__":
    main()
