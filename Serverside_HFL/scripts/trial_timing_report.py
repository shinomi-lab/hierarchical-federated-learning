#!/usr/bin/env python3
"""無人試行の出力ディレクトリから、全端末の学習区間（fix-audit-20261026 N1）を再集計する。

  Serverside_HFL/.venv/bin/python3 Serverside_HFL/scripts/trial_timing_report.py --trials-dir experiment_results/trials/<日時>

マニフェスト（manifests/*.json）の session_complete と、台帳（ledgers/<run_tag>_a<n>/<端末>.raw.jsonl、全ての版）から、
ランナーと同じ定義（scripts/trial_runner/timing.py）で train_span_s を求める。条件ごとの平均は有効な試行だけ。
Sync と Async の条件が両方あれば、同じ遅延条件どうし（名前の sync/async 以外が同じ）の比 Sync/Async を出す。
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

from scripts.trial_runner.timing import train_spans


def _read_jsonl(path: Path):
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            jo = json.loads(line)
        except ValueError:
            continue
        if isinstance(jo, dict):
            out.append(jo)
    return out


def collect(trials_dir):
    d = Path(trials_dir)
    rows = []
    for p in sorted((d / "manifests").glob("*.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        ldir = d / "ledgers" / f"{m['run_tag']}_a{m.get('attempt', 1)}"
        ledgers = {f.name[: -len(".raw.jsonl")]: _read_jsonl(f) for f in ldir.glob("*.raw.jsonl")} if ldir.is_dir() else {}
        t = train_spans(m.get("session_complete") or [], ledgers)
        rows.append({"run_tag": m["run_tag"], "condition": m["condition"]["name"], "mode": m["condition"].get("mode"),
                     "valid": m.get("valid"), "train_span_s": t["train_span_s"], "per_terminal_s": t["per_terminal_s"],
                     "missing": t["missing"], "duration_s": m.get("duration_s")})
    return rows


def condition_means(rows):
    out = {}
    for r in rows:
        if r["valid"] and r["train_span_s"] is not None:
            out.setdefault(r["condition"], []).append(r["train_span_s"])
    return {k: (statistics.mean(v), (statistics.stdev(v) if len(v) > 1 else 0.0), len(v)) for k, v in out.items()}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--trials-dir", required=True)
    a = p.parse_args(argv)
    rows = collect(a.trials_dir)
    print("| 試行 | 条件 | 有効 | 学習区間 [s] | 端末ごと [s] | 欠け | ランナー [s]（参考） |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        per = "・".join(f"{v}" for _, v in sorted(r["per_terminal_s"].items()))
        print(f"| {r['run_tag']} | {r['condition']} | {r['valid']} | {r['train_span_s']} | {per} | {','.join(r['missing']) or '-'} | {r['duration_s']} |")
    means = condition_means(rows)
    print()
    for k, (mu, sd, n) in means.items():
        print(f"{k}: 学習区間の平均 {mu:.1f} s（標準偏差 {sd:.1f}、n={n}）")
    for k in means:
        if "sync" in k and "async" not in k:
            ak = k.replace("sync", "async", 1)
            if ak in means:
                print(f"比 Sync/Async（{k} / {ak}）: {means[k][0] / means[ak][0]:.2f}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
