#!/usr/bin/env python3
"""SP5: 実機の無人試行ランナー（条件表 trials.yaml から試行を順に実行・検証・記録する）。

  cd Serverside_HFL
  .venv/bin/python3 scripts/run_trials.py scripts/trials.example.yaml --dry-run     # 実行順と起動引数を表示するだけ
  caffeinate -i .venv/bin/python3 scripts/run_trials.py trials.yaml                  # 夜間に回す
  .venv/bin/python3 scripts/run_trials.py trials.yaml --resume ../experiment_results/trials/<日時>   # 中断から再開

毎試行、scripts/prepare_server_run.sh の実行ディレクトリ（settings.runs_dir）からサーバを起動し、試行の後に止める。
リポジトリの Serverside_HFL でサーバを動かしていないこと（ポート 8000〜8002 が空いていること）。
手順: docs/unattended_trials_runbook.md、設計: docs/superpowers/specs/2026-10-13-sp5-unattended-trial-runner-design.md
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.trial_runner import adapters, plan, runner  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
APK = REPO / "HFL_terminals" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"


def _sha256(p: Path) -> Optional[str]:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def remaining(trials: List[plan.Trial], done_manifests: List[dict]) -> List[plan.Trial]:
    """既にある有効な試行の数だけ、各条件の先頭の試行を飛ばす（--resume）。"""
    left = {}
    for m in done_manifests:
        if m.get("valid"):
            name = m["condition"]["name"]
            left[name] = left.get(name, 0) + 1
    out = []
    for t in trials:
        if left.get(t.condition.name, 0) > 0:
            left[t.condition.name] -= 1
            continue
        out.append(t)
    return out


def _context(trials_path: Path, seed: int) -> dict:
    git = lambda *a: subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True).stdout.strip()  # noqa: E731
    return {"git_commit": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain", "--", "Serverside_HFL",
                                                                        "HFL_terminals", "scripts")),
            "apk_sha256": _sha256(APK), "trials_yaml_sha256": _sha256(trials_path), "seed": seed}


class _Clock:
    now = staticmethod(datetime.now)
    sleep = staticmethod(time.sleep)


def main(argv=None, delay_available: Optional[Callable[[], bool]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trials", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="実行順とサーバの起動引数を表示して終わる（adb・サーバに触れない）")
    ap.add_argument("--out", type=Path, help="出力先（既定 experiment_results/trials/<日時>）")
    ap.add_argument("--resume", type=Path, help="この出力先の有効な試行を数え、残りだけを回す")
    a = ap.parse_args(argv)

    if delay_available is None:
        delay_available = adapters.DelayHook(None, None).available
    try:
        settings, conditions = plan.load_trials(a.trials, delay_available=(
            delay_available() if any("delay" in str(x) for x in a.trials.read_text(encoding="utf-8").splitlines()
                                     if not x.lstrip().startswith("#")) else False))
    except plan.TrialsError as e:
        print(f"条件表の誤り: {e}", file=sys.stderr)
        return 2
    missing = [t for t in settings["terminals"] if t not in (settings.get("devices") or {})]
    if missing:
        print(f"条件表の誤り: settings.devices に adb シリアルが無い端末: {missing}", file=sys.stderr)
        return 2

    trials = plan.order_trials(conditions, settings["seed"])
    out = a.resume or a.out or REPO / "experiment_results" / "trials" / datetime.now().strftime("%Y%m%d_%H%M")
    if a.resume:
        done = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((a.resume / "manifests").glob("*.json"))]
        trials = remaining(trials, done)

    print(f"{len(trials)} 試行（seed={settings['seed']}、端末 {len(settings['terminals'])} 台、出力 {out}）")
    for t in trials:
        c = t.condition
        print(f"  {t.run_tag:<28} block={t.block} mode={c.mode:<5} baseline={c.baseline:<6} cycles={c.num_cycles} delay={c.delay}")
    for mode in sorted({t.condition.mode for t in trials}):
        env, argv_s = adapters.server_start_cmd(Path("<RUN>"), mode, settings["server"])
        print(f"  サーバ（{mode}）: HFL_MODE={env['HFL_MODE']} {' '.join(argv_s)}")
    if a.dry_run:
        return 0

    out.mkdir(parents=True, exist_ok=True)
    notifier = adapters.Notifier(out / "run.log")
    deps = runner.Deps(adb=adapters.Adb(), server=adapters.Server(settings["runs_dir"]),
                       delay=adapters.DelayHook(settings.get("delay_targets"), settings.get("delay_probes")),
                       notifier=notifier, clock=_Clock(), reconcile=_reconcile(),
                       context=_context(a.trials, settings["seed"]))
    notifier.log(f"開始 commit={deps.context['git_commit'][:8]} dirty={deps.context['git_dirty']} "
                 f"apk={str(deps.context['apk_sha256'])[:12]} 試行 {len(trials)}")
    ms = runner.run_all(trials, settings, deps, out)
    done = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((out / "manifests").glob("*.json"))]
    ok = {c.name: sum(1 for m in done if m["valid"] and m["condition"]["name"] == c.name) for c in conditions}
    return 0 if all(ok[c.name] >= c.n for c in conditions) else 1  # 全条件が有効数 n に達したら 0


def _reconcile():
    from scripts.reconcile_metrics import reconcile
    return reconcile


if __name__ == "__main__":
    sys.exit(main())
