#!/usr/bin/env python3
"""
docker/analyze_ch4_kc1.py — 第4章 Async を K_CENTRAL=1（実機と同じ）で測り直した結果の集計。
  python3 docker/analyze_ch4_kc1.py --kc1 experiment_results/docker_kc1_20261015 \
    --base experiment_results/docker_rerun_20261015 --poll experiment_results/docker_poll_20261015 [--out report.md]

- N ごとに Async(K_CENTRAL=1)・Async(K_CENTRAL=2、10/15 夜)・Sync（POLL_S=5,2,1）の完了時間を並べる
- 検定（Welch、Holm は比較の種類ごとに N 全体）: KC1 対 KC2、Sync 5 s 対 KC1、Sync 1 s 対 KC1
- 中央の集約回数（中央ログの「K_CENTRAL=k 達成」）を試行ごとに数える
"""
from __future__ import annotations

import argparse
import csv
import re
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_ch4_rerun import hedges_g, holm, welch  # noqa: E402


def load(path: Path, mode: str, k_central: int | None = None) -> dict:
    out = {}
    if not path.exists():
        return out
    for r in csv.DictReader(open(path)):
        if r["valid"] == "1" and r["mode"] == mode and (k_central is None or int(r["k_central"]) == k_central):
            out.setdefault(int(r["n"]), []).append(float(r["elapsed_s"]))
    return out


def central_aggs(root: Path, k_central: int) -> dict:
    """N → 試行ごとの中央の集約回数（有効な async 試行のみ）。"""
    out = {}
    pat = re.compile(rf"K_CENTRAL={k_central} 達成")
    for r in csv.DictReader(open(root / "trials.csv")):
        if r["valid"] != "1" or r["mode"] != "async":
            continue
        log = root / "trials" / r["run_id"] / "server_central.log"
        if log.exists():
            out.setdefault(int(r["n"]), []).append(len(pat.findall(log.read_text(errors="replace"))))
    return out


def ms(v):
    return f"{st.mean(v):.1f}±{st.stdev(v):.1f} ({len(v)})" if len(v) >= 2 else "—"


def compare(name, pairs):
    """pairs: [(n, a, b)] → 行のリスト（Holm は N 全体）。比 = mean(a)/mean(b)。"""
    ok = [(n, a, b) for n, a, b in pairs if len(a) >= 2 and len(b) >= 2]
    ps = [welch(a, b)[2] for _, a, b in ok]
    adj = holm(ps) if ps else []
    rows = []
    for (n, a, b), p, pa in zip(ok, ps, adj):
        rows.append(f"| {name} | {n} | {st.mean(a) / st.mean(b):.2f} | {p:.2g} | {pa:.2g} | {hedges_g(a, b):+.2f} |")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kc1", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--poll", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    kc1_dir, base, poll = Path(a.kc1), Path(a.base), Path(a.poll)
    kc1 = load(kc1_dir / "trials.csv", "async", 1)
    kc2 = load(base / "trials.csv", "async", 2)
    sync = {5: load(base / "trials.csv", "sync"),
            2: load(poll / "sync_poll2" / "trials.csv", "sync"),
            1: load(poll / "sync_poll1" / "trials.csv", "sync")}
    agg1, agg2 = central_aggs(kc1_dir, 1), central_aggs(base, 2)
    rows_all = list(csv.DictReader(open(kc1_dir / "trials.csv")))
    n_valid = sum(r["valid"] == "1" for r in rows_all)
    ns = sorted(kc1)

    L = ["# 第4章 Async を K_CENTRAL=1 で測り直した結果 — 集計", "",
         f"- K_CENTRAL=1: `{kc1_dir}`（{n_valid}/{len(rows_all)} 試行が有効）。K_CENTRAL=2 と Sync 5 s: `{base}`。Sync 2 s・1 s: `{poll}`。",
         "- 完了時間 = 10 サイクル、試行単位の平均±SD (n)。エッジ閾値 N/2、教師 default、5 エポック（条件は 10/15 と同じで K_CENTRAL だけが違う）。", "",
         "## 完了時間", "",
         "| N | Async KC1 | Async KC2 | Sync 5 s | Sync 2 s | Sync 1 s | Sync5/KC1 | Sync1/KC1 | KC1/KC2 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for n in ns:
        m1 = st.mean(kc1[n])
        r = lambda v, d: f"{st.mean(v) / d:.2f}" if len(v) >= 2 else "—"  # noqa: E731
        L.append(f"| {n} | {ms(kc1[n])} | {ms(kc2.get(n, []))} | {ms(sync[5].get(n, []))} | {ms(sync[2].get(n, []))} | "
                 f"{ms(sync[1].get(n, []))} | {r(sync[5].get(n, []), m1)} | {r(sync[1].get(n, []), m1)} | "
                 f"{r(kc1[n], st.mean(kc2[n])) if len(kc2.get(n, [])) >= 2 else '—'} |")
    L += ["", "## 検定（Welch、Holm は比較ごとに N 全体）", "",
          "| 比較 | N | 比 (a/b) | p | p (Holm) | Hedges g |", "|---|---|---|---|---|---|"]
    L += compare("KC1 対 KC2", [(n, kc1[n], kc2.get(n, [])) for n in ns])
    L += compare("Sync 5 s 対 KC1", [(n, sync[5].get(n, []), kc1[n]) for n in ns])
    L += compare("Sync 1 s 対 KC1", [(n, sync[1].get(n, []), kc1[n]) for n in ns])
    L += ["", "## 中央の集約回数（1 試行あたり、平均±SD）", "", "| N | KC1 | KC2 |", "|---|---|---|"]
    for n in ns:
        L.append(f"| {n} | {ms(agg1.get(n, []))} | {ms(agg2.get(n, []))} |")
    text = "\n".join(L) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
