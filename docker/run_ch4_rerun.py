#!/usr/bin/env python3
"""
docker/run_ch4_rerun.py — 第4章 Docker 再計測（Async vs Sync）の一括実行。
設計: docs/superpowers/specs/2026-10-15-docker-ch4-rerun-design.md

締切まで「6 条件（async/sync × N=4,6,8）を周ごとに順序を入れ替えて 1 回ずつ」を周回する。
主比較が各 --min-reps 試行に達したら --extra-n の N も周回に加える。
1 試行 = サーバを状態ごと作り直す → 条件の反映を確認 → 端末 N 台 → 全端末の終了を待つ → 回収 → 有効性判定。

標準ライブラリのみ（システムの python3 で動く）。例:
  python3 docker/run_ch4_rerun.py --deadline 07:00
  python3 docker/run_ch4_rerun.py --smoke          # N=4 の async/sync を 1 回ずつ
  python3 docker/run_ch4_rerun.py --modes async --k-central 1   # Async を実機と同じ K_CENTRAL=1 で
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import random
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "terminal_client"))
from protocol import trial_validity  # noqa: E402  (torch は使わない関数だけ)

COMPOSE = ["docker", "compose", "-f", str(REPO / "docker" / "docker-compose.yml")]
SERVICES = ["redis", "central", "edge-00", "edge-01"]
NETWORK = "hfl_hfl_net"
IMAGE = "hfl-terminal-00"
CPUS = ["1.0", "0.7", "0.5", "0.8", "0.6"]  # 旧実験と同じ循環
APPS = ["browser", "video", "call", "other"]
EDGE_PORTS = {"edge-00": 8001, "edge-01": 8002}
CSV_FIELDS = ["run_id", "rep", "mode", "n", "k_edge", "k_central", "seed_base", "cycles", "epochs",
              "elapsed_s", "val_acc_mean", "val_acc_std", "n_terminals_ok", "sync_wait_s_mean",
              "valid", "invalid_reason", "git_commit", "started_at", "poll_s"]


def sh(cmd, check=True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True, **kw)


def log(msg: str, logf: Path) -> None:
    line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(logf, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def remove_terminals() -> None:
    names = sh(["docker", "ps", "-aq", "--filter", "name=hfl-rr-"], check=False).stdout.split()
    if names:
        sh(["docker", "rm", "-f", *names], check=False)


def central_k_values(central_log: str) -> set:
    """中央ログの async イベント（JSON）に記録された K_CENTRAL の値の集合。sync では空。"""
    return {int(v) for v in re.findall(r'"K_CENTRAL": (\d+)', central_log)}


def server_down() -> None:
    sh(COMPOSE + ["down", "-v", "--remove-orphans"], check=False)


def server_up(mode: str, k_edge: int, k_central: int) -> dict:
    env = {"HFL_MODE": mode, "K_CENTRAL": str(k_central), "EDGE_AGG_THRESHOLD": str(k_edge)}
    import os
    sh(COMPOSE + ["up", "-d", *SERVICES], env={**os.environ, **env})
    # 条件が本当に反映されたか（旧実験は反映されていなかった）とモデル配布の準備を確かめる
    deadline = time.time() + 180
    last = {}
    while time.time() < deadline:
        try:
            ok = True
            for eid, port in EDGE_PORTS.items():
                m = http_json(f"http://127.0.0.1:{port}/api/v1/meta")
                last[eid] = {k: m.get(k) for k in ("round", "hfl_mode", "aggregation_threshold")}
                pkg = http_json(f"http://127.0.0.1:{port}/send_to_device")
                ok &= (m.get("hfl_mode") == mode and int(m.get("aggregation_threshold") or 0) == k_edge
                       and bool((pkg.get("links") or {}).get("model_bin")))
            if ok:
                # K_CENTRAL は中央の API に出ないので、コンテナの環境変数で確かめる
                kc = sh(["docker", "exec", "hfl-central", "printenv", "K_CENTRAL"], check=False).stdout.strip()
                if kc != str(k_central):
                    raise RuntimeError(f"K_CENTRAL not applied: want {k_central}, central has {kc!r}")
                last["central"] = {"K_CENTRAL": int(kc)}
                return last
        except Exception as e:  # 起動中
            last["error"] = str(e)[:200]
        time.sleep(3)
    raise RuntimeError(f"server not ready or condition not applied: {last}")


def start_terminals(run_id: str, n: int, seed_base: int, cycles: int, epochs: int, logs_dir: Path,
                    poll_s: float) -> list:
    names = []
    for i in range(n):
        tid = f"rr-t{i:02d}"
        edge = "edge-00" if i < n // 2 else "edge-01"
        name = f"hfl-{tid}"
        sh(["docker", "run", "-d", "--name", name, "--network", NETWORK, f"--cpus={CPUS[i % len(CPUS)]}",
            "-e", f"TERMINAL_ID={tid}", "-e", f"EDGE_URL=http://{edge}:8001",
            "-e", f"APP_CATEGORY={APPS[i % len(APPS)]}", "-e", f"SEED={seed_base + i}",
            "-e", f"NUM_CYCLES={cycles}", "-e", f"EPOCHS={epochs}", "-e", f"POLL_S={poll_s}",
            "-v", f"{REPO / 'training_data_list' / 'default'}:/app/training_data:ro",
            "-v", f"{logs_dir}:/app/logs",
            "--label", f"hfl_run_id={run_id}", IMAGE])
        names.append(name)
    return names


def wait_terminals(names: list, timeout_s: float) -> tuple[bool, dict]:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        states = {}
        for nm in names:
            out = sh(["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}} {{.State.FinishedAt}}", nm],
                     check=False).stdout.split()
            states[nm] = out
        if all(s and s[0] == "exited" for s in states.values()):
            return True, states
        time.sleep(3)
    return False, states


def parse_docker_time(s: str) -> float:
    # 例: 2026-10-15T01:23:45.123456789Z
    base, _, frac = s.rstrip("Z").partition(".")
    t = dt.datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
    return t + (float("0." + frac[:6]) if frac else 0.0)


def read_cycles(logs_dir: Path) -> dict:
    by_t = {}
    for p in sorted(logs_dir.glob("*.jsonl")):
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        by_t[p.stem] = [r for r in rows if r.get("kind") == "cycle"]
    return by_t


def run_trial(rep: int, mode: str, n: int, args, out: Path, commit: str, logf: Path) -> dict:
    k_edge, k_central = max(1, n // 2), args.k_central
    seed_base = rep * 100
    started = dt.datetime.now()
    run_id = f"{started:%Y%m%d_%H%M%S}_{mode}_N{n}_r{rep:02d}"
    tdir = out / "trials" / run_id
    logs_dir = tdir / "terminals"
    logs_dir.mkdir(parents=True, exist_ok=True)
    row = {"run_id": run_id, "rep": rep, "mode": mode, "n": n, "k_edge": k_edge, "k_central": k_central,
           "seed_base": seed_base, "cycles": args.cycles, "epochs": args.epochs, "elapsed_s": "",
           "val_acc_mean": "", "val_acc_std": "", "n_terminals_ok": 0, "sync_wait_s_mean": "",
           "valid": 0, "invalid_reason": "", "git_commit": commit, "started_at": started.isoformat(timespec="seconds"),
           "poll_s": args.poll}
    log(f"trial {run_id}: mode={mode} N={n} K_EDGE={k_edge} K_CENTRAL={k_central} POLL_S={args.poll}", logf)
    try:
        remove_terminals()
        server_down()
        applied = server_up(mode, k_edge, k_central)
        (tdir / "server_condition.json").write_text(json.dumps(applied, indent=1))
        t_start = time.time()
        names = start_terminals(run_id, n, seed_base, args.cycles, args.epochs, logs_dir, args.poll)
        done, states = wait_terminals(names, args.trial_timeout)
        for nm in names:
            (tdir / f"{nm}.log").write_text(sh(["docker", "logs", nm], check=False).stdout
                                             + sh(["docker", "logs", nm], check=False).stderr)
        for svc in ("central", "edge-00", "edge-01"):
            r = sh(COMPOSE + ["logs", "--no-color", svc], check=False)
            (tdir / f"server_{svc}.log").write_text(r.stdout + r.stderr)
            if svc == "central":
                k_seen = central_k_values(r.stdout + r.stderr)
        if not done:
            row["invalid_reason"] = "timeout"
            return row
        finished = [parse_docker_time(s[2]) for s in states.values()]
        row["elapsed_s"] = round(max(finished) - t_start, 1)
        row["n_terminals_ok"] = sum(1 for s in states.values() if s[1] == "0")
        cycles = read_cycles(logs_dir)
        last_acc = [rows[-1]["val_acc"] for rows in cycles.values() if rows]
        if last_acc:
            row["val_acc_mean"] = round(statistics.mean(last_acc), 4)
            row["val_acc_std"] = round(statistics.stdev(last_acc), 4) if len(last_acc) > 1 else 0.0
        waits = [r["wait_s"] for rows in cycles.values() for r in rows]
        row["sync_wait_s_mean"] = round(statistics.mean(waits), 2) if waits else ""
        if row["n_terminals_ok"] != n:
            row["invalid_reason"] = "terminal_error"
        elif len(cycles) != n:
            row["invalid_reason"] = "missing_logs"
        elif any(r.get("upload_status", 0) >= 300 for rows in cycles.values() for r in rows):
            row["invalid_reason"] = "upload_failed"
        elif any(r.get("wait_timeout") for rows in cycles.values() for r in rows):
            row["invalid_reason"] = "wait_timeout"
        elif mode == "async" and k_seen != {k_central}:
            row["invalid_reason"] = f"k_central_mismatch: {sorted(k_seen)}"
        else:
            ok, why = trial_validity(cycles, args.cycles, hfl_mode=mode)
            row["valid"], row["invalid_reason"] = int(ok), why
        return row
    except Exception as e:
        row["invalid_reason"] = f"error: {str(e)[:150]}"
        return row
    finally:
        remove_terminals()
        server_down()
        log(f"  -> elapsed={row['elapsed_s']} valid={row['valid']} {row['invalid_reason']} "
            f"val_acc={row['val_acc_mean']} wait_mean={row['sync_wait_s_mean']}", logf)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deadline", default="07:00", help="この時刻（次に来る HH:MM）以降は新しい試行を始めない")
    ap.add_argument("--out", default=str(REPO / "experiment_results" / f"docker_rerun_{dt.date.today():%Y%m%d}"))
    ap.add_argument("--cycles", type=int, default=10)
    ap.add_argument("--epochs", type=int, default=5)
    ap.add_argument("--ns", default="4,6,8")
    ap.add_argument("--extra-n", default="10,14")
    ap.add_argument("--min-reps", type=int, default=10)
    ap.add_argument("--trial-timeout", type=float, default=1800)
    ap.add_argument("--smoke", action="store_true", help="N=4 の async/sync を 1 回ずつ")
    ap.add_argument("--poll", type=float, default=5.0, help="端末のラウンド確認間隔 [s]（既定 5 = 実機 APK）")
    ap.add_argument("--modes", default="async,sync")
    ap.add_argument("--k-central", type=int, default=2,
                    help="async の中央の集約数 K_CENTRAL（既定 2 = 10/15 再計測。実機は 1）")
    ap.add_argument("--max-reps", type=int, default=0, help="この周回数で終える（0 = 締切まで）")
    args = ap.parse_args()

    out = Path(args.out).resolve()  # docker -v は絶対パスが必要
    out.mkdir(parents=True, exist_ok=True)
    logf = out / "run.log"
    trials_csv = out / "trials.csv"
    if not trials_csv.exists():
        with open(trials_csv, "w", newline="") as f:
            csv.DictWriter(f, CSV_FIELDS).writeheader()
    commit = sh(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], check=False).stdout.strip()
    dirty = sh(["git", "-C", str(REPO), "status", "--porcelain", "terminal_client", "docker", "Serverside_HFL",
                "Sim_HFL/sim"], check=False).stdout.strip()
    if dirty:
        commit += "-dirty"

    now = dt.datetime.now()
    hh, mm = map(int, args.deadline.split(":"))
    deadline = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if deadline <= now:
        deadline += dt.timedelta(days=1)
    primary = [int(x) for x in args.ns.split(",")]
    extra = [int(x) for x in args.extra_n.split(",") if x]
    modes = args.modes.split(",")
    log(f"start commit={commit} deadline={deadline:%m-%d %H:%M} primary={primary} extra={extra} "
        f"cycles={args.cycles} epochs={args.epochs} poll={args.poll} k_central={args.k_central} modes={modes} out={out}", logf)

    done_valid: dict = {}
    durations: dict = {}
    rep = 0
    while True:
        rep += 1
        ns = [4] if args.smoke else primary + (extra if all(done_valid.get((m, n), 0) >= args.min_reps
                                                             for m in modes for n in primary) else [])
        conds = [(m, n) for n in ns for m in modes]
        random.Random(rep).shuffle(conds)
        ran = 0
        for mode, n in conds:
            est = max(durations.get((mode, n), [600.0])) + 120
            if dt.datetime.now() + dt.timedelta(seconds=est) > deadline:
                log(f"deadline: skip {mode} N={n} (est {est:.0f}s)", logf)
                continue
            t0 = time.time()
            ran += 1
            row = run_trial(rep, mode, n, args, out, commit, logf)
            durations.setdefault((mode, n), []).append(time.time() - t0)
            with open(trials_csv, "a", newline="") as f:
                csv.DictWriter(f, CSV_FIELDS).writerow(row)
            if row["valid"]:
                done_valid[(mode, n)] = done_valid.get((mode, n), 0) + 1
        log(f"rep {rep} done. valid counts: {dict(sorted(done_valid.items()))}", logf)
        if args.smoke or ran == 0 or (args.max_reps and rep >= args.max_reps) or dt.datetime.now() + dt.timedelta(minutes=5) > deadline:
            break
    log("finished", logf)


if __name__ == "__main__":
    main()
