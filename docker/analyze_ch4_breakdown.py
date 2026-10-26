#!/usr/bin/env python3
"""
docker/analyze_ch4_breakdown.py — 第4章 再計測の内訳（端末 JSONL・中央ログから）。
  python3 docker/analyze_ch4_breakdown.py experiment_results/docker_rerun_20261015 [--out breakdown.md] [--fig fig.png]

analyze_ch4_rerun.py（完了時間の検定）の数字が何から来ているかを確かめる:
- 配布モデルが届いたか（cycle>=2 の DOWNLOADED 回数）・ラウンドの進み・アップロード再送
- Sync の待ちの割合と、待ちが確認間隔（POLL 5 秒）の何回分か
- 確認間隔を除いた Sync の時間の見積り = Σ_cycle（その cycle で最も遅い端末の作業時間）。
  作業時間 = t_end - wait_s - t_start（ダウンロード・学習・アップロード）。集約の遅れは中央ログの
  バッチ書き込み時刻（秒単位）から別に示す。
- 端末の val_acc の分布（取りうる値が少ないことの確認）
標準ライブラリのみ（--fig のときだけ matplotlib）。
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import glob
import json
import re
import statistics as st
from pathlib import Path

POLL_S = 5.0  # trials.csv に poll_s 列が無い（夜の計測）ときの既定


def load_cycles(run_dir: Path):
    out = []
    for f in sorted(glob.glob(str(run_dir / "terminals" / "*.jsonl"))):
        for line in open(f):
            row = json.loads(line)
            if row.get("kind") == "cycle":
                out.append(row)
    return out


def batch_times(central_log: Path):
    """中央が書いたバッチ（ラウンド k のモデル）の時刻。ディレクトリ名 YYYYmmdd_HHMMSS_rK（コンテナ内 UTC、秒単位）。"""
    bt = {}
    if not central_log.exists():
        return bt
    pat = re.compile(r"バッチモデルを書き込みました: /app/dist/(\d{8}_\d{6})_r(\d+)")
    for m in pat.finditer(central_log.read_text(errors="replace")):
        t = dt.datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=dt.timezone.utc).timestamp()
        bt.setdefault(int(m.group(2)), t)
    return bt


def ms(v, d=1):
    return f"{st.mean(v):.{d}f}±{st.stdev(v):.{d}f}" if len(v) >= 2 else (f"{v[0]:.{d}f}" if v else "—")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("result_dir")
    ap.add_argument("--out")
    ap.add_argument("--fig")
    args = ap.parse_args()
    root = Path(args.result_dir)
    rows = [r for r in csv.DictReader(open(root / "trials.csv")) if r["valid"] == "1"]

    S = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        key = (r["mode"], int(r["n"]))
        S[key]["poll"].append(float(r.get("poll_s") or POLL_S))
        run_dir = root / "trials" / r["run_id"]
        cyc = load_cycles(run_dir)
        s = S[key]
        s["elapsed"].append(float(r["elapsed_s"]))
        by_t = collections.defaultdict(list)
        by_c = collections.defaultdict(list)
        for c in cyc:
            by_t[c["terminal_id"]].append(c)
            work = c["t_end"] - c["wait_s"] - c["t_start"]
            by_c[c["cycle"]].append((work, c))
            s["work"].append(work)
            s["wait"].append(c["wait_s"])
            s["retry"].append(int(c["upload_attempts"] > 1))
            s["non200"].append(int(c["upload_status"] != 200))
            s["timeout"].append(int(bool(c["wait_timeout"])))
        for tc in by_t.values():
            s["dl"].append(sum(c["init_source"] == "DOWNLOADED" for c in tc if c["cycle"] >= 2))
            s["final_acc"].append(tc[-1]["val_acc"])
        s["max_round"].append(max((c["round_after"] or 0) for c in cyc))
        s["meta429"].append(sum(p.read_text(errors="replace").count('"GET /api/v1/meta HTTP/1.1" 429')
                                for p in run_dir.glob("server_edge-*.log")))
        s["poll_free"].append(sum(max(w for w, _ in v) for v in by_c.values()))
        if key[0] == "sync":
            bt = batch_times(run_dir / "server_central.log")
            for v in by_c.values():
                last_up = max(c["t_end"] - c["wait_s"] for _, c in v)
                nxt = v[0][1]["round_before"] + 1
                if nxt in bt:
                    s["agg_lag"].append(bt[nxt] - last_up)

    keys = sorted(S, key=lambda k: (k[1], k[0]))
    L = [f"# 第4章 Docker 再計測 — 内訳（{root}）", "",
         f"有効試行 {len(rows)}。端末 JSONL（`trials/<run_id>/terminals/`）と中央ログから計算。"
         f"再計算: `python3 docker/analyze_ch4_breakdown.py {root}`", "",
         "## 1. HFL として動いたか", "",
         "| 条件 | 試行 | DOWNLOADED / 端末（cycle 2〜10、最小） | 到達ラウンド | 再送サイクル | 200 以外 | 待ちタイムアウト | meta の 429 |",
         "|---|---|---|---|---|---|---|---|"]
    for k in keys:
        s = S[k]
        L.append(f"| {k[0]} N={k[1]} | {len(s['elapsed'])} | {st.mean(s['dl']):.2f}（{min(s['dl'])}） | "
                 f"{min(s['max_round'])}〜{max(s['max_round'])} | {sum(s['retry'])} | {sum(s['non200'])} | {sum(s['timeout'])} | {sum(s['meta429'])} |")
    L += ["", "## 2. 時間の内訳", "",
          "作業 = 1 サイクルのダウンロード・学習・アップロード。確認間隔なしの見積り = Σ_cycle（その cycle で最も遅い端末の作業）。",
          "Sync の集約の遅れ = 中央のバッチ書き込み時刻（秒単位で切り捨て）−その cycle の最後のアップロード完了。", "",
          "| 条件 | 完了時間 | 作業/サイクル | 待ち/サイクル | 待ちの割合 | 待ちが確認 1 回以内（≤POLL+0.6 s） | 確認間隔なしの見積り | 集約の遅れ（中央値, 最大） |",
          "|---|---|---|---|---|---|---|---|"]
    for k in keys:
        s = S[k]
        share = sum(s["wait"]) / (sum(s["wait"]) + sum(s["work"]))
        poll = max(s["poll"])
        one = sum(w <= poll + 0.6 for w in s["wait"]) / len(s["wait"]) if k[0] == "sync" else float("nan")
        lag = f"{st.median(s['agg_lag']):.2f}, {max(s['agg_lag']):.2f}" if s["agg_lag"] else "—"
        L.append(f"| {k[0]} N={k[1]} | {ms(s['elapsed'])} | {st.mean(s['work']):.2f} | {st.mean(s['wait']):.2f} | "
                 f"{share:.0%} | {'—' if k[0] == 'async' else f'{one:.0%}'} | {ms(s['poll_free'])} | {lag} |")
    L += ["", "## 3. 端末の最終 val_acc の分布", "", "| 条件 | 値（端末数） |", "|---|---|"]
    for k in keys:
        cnt = collections.Counter(round(x, 4) for x in S[k]["final_acc"]).most_common()
        L.append(f"| {k[0]} N={k[1]} | " + "、".join(f"{v}（{c}）" for v, c in cnt) + " |")
    text = "\n".join(L) + "\n"
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")

    if args.fig:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        ns = sorted({n for _, n in S})
        fig, ax = plt.subplots(figsize=(6, 4))
        for mode, mk, col in (("async", "o", "tab:blue"), ("sync", "s", "tab:red")):
            xs = [n for n in ns if (mode, n) in S]
            ax.errorbar(xs, [st.mean(S[(mode, n)]["elapsed"]) for n in xs],
                        yerr=[st.stdev(S[(mode, n)]["elapsed"]) for n in xs], marker=mk, color=col,
                        capsize=3, label=f"{mode.capitalize()} (measured)")
        xs = [n for n in ns if ("sync", n) in S]
        ax.plot(xs, [st.mean(S[("sync", n)]["poll_free"]) for n in xs], "s--", color="tab:red", alpha=0.5,
                mfc="none", label="Sync without 5 s poll (estimate)")
        ax.set_xlabel("Number of terminals N")
        ax.set_ylabel("Time to finish 10 cycles [s]")
        ax.set_ylim(bottom=0)
        ax.set_xticks(ns)
        ax.grid(alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(args.fig, dpi=150)


if __name__ == "__main__":
    main()
