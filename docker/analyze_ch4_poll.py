#!/usr/bin/env python3
"""
docker/analyze_ch4_poll.py — 第4章 確認間隔（POLL_S）の追加計測の集計。
  python3 docker/analyze_ch4_poll.py --base experiment_results/docker_rerun_20261015 \
    --poll experiment_results/docker_poll_20261015 [--out poll.md] [--fig fig.png]

--base: 夜通しの計測（Async と Sync POLL_S=5）。--poll: 下に sync_poll2/・sync_poll1/・async_check/。
- N ごとに Async と Sync（POLL_S=5,2,1）の完了時間を並べ、Async 対 Sync(各 POLL_S) を Welch（Holm は POLL_S ごとに N 全体）
- async_check（追加計測と同じ時間帯の Async）と夜の Async を比べ、時間帯によるずれがないかを確かめる
"""
from __future__ import annotations

import argparse
import csv
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_ch4_rerun import hedges_g, holm, welch  # noqa: E402


def load(path: Path, mode: str) -> dict:
    out = {}
    if not path.exists():
        return out
    for r in csv.DictReader(open(path)):
        if r["valid"] == "1" and r["mode"] == mode:
            out.setdefault(int(r["n"]), []).append(float(r["elapsed_s"]))
    return out


def ms(v):
    return f"{st.mean(v):.1f}±{st.stdev(v):.1f} ({len(v)})" if len(v) >= 2 else "—"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--poll", required=True)
    ap.add_argument("--out")
    ap.add_argument("--fig")
    a = ap.parse_args()
    base, poll = Path(a.base), Path(a.poll)
    asy = load(base / "trials.csv", "async")
    sync = {5: load(base / "trials.csv", "sync"),
            2: load(poll / "sync_poll2" / "trials.csv", "sync"),
            1: load(poll / "sync_poll1" / "trials.csv", "sync")}
    chk = load(poll / "async_check" / "trials.csv", "async")
    ns = sorted(set(asy) & set().union(*[set(v) for v in sync.values()]))

    L = [f"# 第4章 確認間隔（POLL_S）の追加計測 — 集計", "",
         f"- 夜: `{base}`（Async・Sync POLL_S=5）。追加: `{poll}`（Sync POLL_S=2,1、Async 確認用）。",
         "- 完了時間 = 10 サイクル、試行単位の平均±SD (n)。比 = Sync/Async。", "",
         "| N | Async | Sync 5 s | Sync 2 s | Sync 1 s | 比 5 s | 比 2 s | 比 1 s |", "|---|---|---|---|---|---|---|---|"]
    for n in ns:
        ma = st.mean(asy[n])
        ratio = lambda p: f"{st.mean(sync[p][n]) / ma:.2f}" if len(sync[p].get(n, [])) >= 2 else "—"
        L.append(f"| {n} | {ms(asy[n])} | {ms(sync[5].get(n, []))} | {ms(sync[2].get(n, []))} | {ms(sync[1].get(n, []))} | "
                 f"{ratio(5)} | {ratio(2)} | {ratio(1)} |")

    L += ["", "## Async 対 Sync（Welch、両側。Holm は POLL_S ごとに N 全体）", "",
          "| POLL_S | N | 差 Sync−Async [s] | t (df) | p | Holm p | Hedges g |", "|---|---|---|---|---|---|---|"]
    for p in (2, 1):
        rows = [(n, *welch(sync[p][n], asy[n]), hedges_g(sync[p][n], asy[n]))
                for n in ns if len(sync[p].get(n, [])) >= 2]
        adj = holm([r[3] for r in rows]) if rows else []
        for (n, t, df, pv, g), pa in zip(rows, adj):
            d = st.mean(sync[p][n]) - st.mean(asy[n])
            L.append(f"| {p} | {n} | {d:+.1f} | {t:.2f} ({df:.1f}) | {pv:.2g} | {pa:.2g} | {g:.2f} |")

    L += ["", "## 時間帯によるずれの確認（Async: 追加計測と同じ時間帯 vs 夜）", "",
          "| N | 夜 | 追加計測の時間帯 | 差 | p |", "|---|---|---|---|---|"]
    for n in ns:
        if len(chk.get(n, [])) >= 2:
            _, _, pv = welch(chk[n], asy[n])
            L.append(f"| {n} | {ms(asy[n])} | {ms(chk[n])} | {st.mean(chk[n]) - st.mean(asy[n]):+.1f} | {pv:.2g} |")
    text = "\n".join(L) + "\n"
    print(text)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")

    if a.fig:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.errorbar(ns, [st.mean(asy[n]) for n in ns], yerr=[st.stdev(asy[n]) for n in ns],
                    marker="o", color="tab:blue", capsize=3, label="Async")
        for p, alpha in ((5, 1.0), (2, 0.7), (1, 0.45)):
            xs = [n for n in ns if len(sync[p].get(n, [])) >= 2]
            ax.errorbar(xs, [st.mean(sync[p][n]) for n in xs], yerr=[st.stdev(sync[p][n]) for n in xs],
                        marker="s", color="tab:red", alpha=alpha, capsize=3, label=f"Sync (poll {p} s)")
        ax.set_xlabel("Number of terminals N")
        ax.set_ylabel("Time to finish 10 cycles [s]")
        ax.set_ylim(bottom=0)
        ax.set_xticks(ns)
        ax.grid(alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(a.fig, dpi=150)


if __name__ == "__main__":
    main()
