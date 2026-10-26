"""全条件×Async/Sync を学習込みで実行し、1試行1行の CSV にする。再開可能。

seed は試行番号から決まる (1000 + rep)。条件・方式に依らない。
"""
from __future__ import annotations

import argparse
import copy
import csv
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from . import conditions as C
from .extract import extract

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["condition", "axis", "level", "mode", "seed", "status", "wall_s",
          "global_rounds", "val_acc_tail", "sat_mean_tail", "sat_hm_tail",
          "staleness_mean", "staleness_p95", "staleness_max", "error"]


def run_one(out_dir: Path, cond: dict, mode: str, seed: int) -> dict:
    row = {"condition": cond["name"], "axis": cond["axis"] or "", "level": cond["level"] or "",
           "mode": mode, "seed": seed, "status": "ok", "error": ""}
    trial_dir = out_dir / "trials" / f"{cond['name']}_{mode}_{seed}"
    cfg = copy.deepcopy(cond["cfg"])
    cfg["experiment"]["seed"] = seed
    cfg["experiment"]["hfl_mode"] = mode
    cfg.setdefault("output", {})
    cfg["output"].update({"results_dir": str(trial_dir), "verbose": False, "save_plots": False})
    t0 = time.time()
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
            cfg_path = f.name
        cmd = [sys.executable, "run_sim.py", "--config", cfg_path, "--seed", str(seed)]
        if mode == "async":
            cmd.append("--async")
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"exit {proc.returncode}: {proc.stderr[-300:]}")
        logs = sorted((trial_dir / "logs").glob("rounds_*.jsonl"))
        if not logs:
            raise RuntimeError("JSONL ログが無い")
        row.update(extract(logs[-1]))
        if row.pop("mode") != mode and mode == "async":
            raise RuntimeError("async を指定したが sync で動いた")
        row["mode"] = mode
    except Exception as e:  # 失敗は行として残す（集計では除外し件数を報告）
        row["status"] = "failed"
        row["error"] = str(e).replace("\n", " ")[:300]
    row["wall_s"] = round(time.time() - t0, 1)
    return row


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--only", default=None, help="条件名をカンマ区切りで限定（試走用）")
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "learning_raw.csv"
    done = set()
    if csv_path.exists():
        with open(csv_path, encoding="utf-8") as f:
            done = {(r["condition"], r["mode"], r["seed"]) for r in csv.DictReader(f) if r["status"] == "ok"}
    conds = C.build_conditions()
    if a.only:
        keep = set(a.only.split(","))
        conds = [c for c in conds if c["name"] in keep]
    jobs = [(c, m, 1000 + r) for r in range(a.reps) for c in conds for m in ("async", "sync")
            if (c["name"], m, str(1000 + r)) not in done]
    print(f"jobs={len(jobs)} (skip {len(done)} done)", flush=True)
    new = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f, ThreadPoolExecutor(a.workers) as ex:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for row in ex.map(lambda j: run_one(out, *j), jobs):
            w.writerow(row)
            f.flush()
            print(row["condition"], row["mode"], row["seed"], row["status"], row["wall_s"], flush=True)


if __name__ == "__main__":
    main()
