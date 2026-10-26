"""1 試行の流れ・再試行・追加試行。設計書 §3.3・§6、計画書 Task 7。

1 試行 = 新しい実行ディレクトリからサーバを起動 → 条件の反映を確認 → 実験制御 → アプリ再起動 → 遅延 → 開始 →
完了待ち → 台帳の回収（このセッションの行だけ）と照合 → 検証 → 遅延を外しサーバを止める（例外時も必ず）。
"""
import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List

from scripts.trial_runner import manifest as mf
from scripts.trial_runner.plan import Trial
from scripts.trial_runner.timing import train_spans
from scripts.trial_runner.validate import Verdict, session_rows, validate_trial

POLL_S = 2  # fix-audit-20261026 N1: 以前は 10（所要時間が 10 秒刻みに丸められていた）
HEALTH_TIMEOUT_S = 90
DEFAULT_RECONCILE_WAIT_S = 60  # After の補完行がサーバに届くのを待つ上限


class TrialAbort(Exception):
    """試行を打ち切る（理由は無効の reason にそのまま入る）。"""


@dataclass
class Deps:
    adb: object
    server: object
    delay: object
    notifier: object
    clock: object
    reconcile: Callable  # (ledger_paths, db_path) -> {"missing_total": int, ...}
    context: dict = field(default_factory=dict)


def _expected_s(trial: Trial, durations: Dict[str, List[float]]) -> float:
    past = durations.get(trial.condition.name)
    return statistics.median(past) if past else trial.condition.num_cycles * 60.0


def _read_jsonl(path: Path) -> List[dict]:
    out = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            jo = json.loads(line)
        except ValueError:
            continue
        if isinstance(jo, dict):
            out.append(jo)
    return out


def run_one(trial: Trial, attempt: int, settings: dict, deps: Deps, out_dir: Path, expected_s: float) -> dict:
    c, srv, clock, log = trial.condition, deps.server, deps.clock, deps.notifier.log
    tids = list(settings["terminals"])
    serial = {t: settings["devices"][t] for t in tids}
    started = clock.now()
    events: List[dict] = []
    ledgers: Dict[str, List[dict]] = {}
    raw_ledgers: Dict[str, List[dict]] = {}  # 全ての版（学習区間の算出に使う。After 補完前の最終サイクルの時刻が要る）
    rc: dict = {"missing_total": 0}
    sc: dict = {}
    delay_applied = False
    run_dir = None
    abort: List[str] = []
    log(f"{trial.run_tag} 試行 {attempt}: mode={c.mode} baseline={c.baseline} cycles={c.num_cycles} delay={c.delay}")
    try:
        run_dir = srv.prepare(f"{trial.run_tag}_a{attempt}")
        srv.start(run_dir, c.mode, settings["server"])
        t_end = clock.now().timestamp() + HEALTH_TIMEOUT_S
        while not srv.healthy():
            if clock.now().timestamp() > t_end:
                raise TrialAbort("server_unhealthy")
            clock.sleep(2)
        sc = srv.condition()
        srv.set_control(c.baseline, c.num_cycles)
        unreachable = [t for t in tids if not deps.adb.ensure_connected(serial[t])]
        if unreachable:
            raise TrialAbort(",".join(f"adb_unreachable:{t}" for t in unreachable))
        for t in tids:
            deps.adb.restart_app(serial[t])
        if c.delay:
            delay_applied = True
            if not deps.delay.apply(c.delay, trial.run_tag):
                raise TrialAbort("delay_verify_failed")
        with ThreadPoolExecutor(max_workers=len(tids)) as ex:  # 開始のずれを小さくする
            list(ex.map(lambda t: deps.adb.auto_start(serial[t], trial.run_tag, c.num_cycles), tids))
        deadline = clock.now().timestamp() + expected_s * settings["timeout_factor"]
        while True:
            done = srv.events("session_complete", trial.run_tag)
            if len({e.get("terminal_id") for e in done} & set(tids)) == len(tids) or clock.now().timestamp() > deadline:
                break
            clock.sleep(POLL_S)
        events = done + srv.events("hfl_mode_mismatch", None)  # サーバは試行ごとに新しいので全件がこの試行のもの
        by_tid = {e.get("terminal_id"): e for e in done}
        ldir = Path(out_dir) / "ledgers" / f"{trial.run_tag}_a{attempt}"
        session_files = []
        for t in tids:
            if t not in by_tid:
                continue
            raw = deps.adb.pull_ledger(serial[t], ldir / f"{t}.raw.jsonl")
            raw_ledgers[t] = _read_jsonl(raw)
            rows = session_rows(raw_ledgers[t], by_tid[t].get("run_id"))
            ledgers[t] = rows
            p = ldir / f"{t}.session.jsonl"  # 照合はこのセッションの行だけ（DB は試行ごとに新しい）
            p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            session_files.append(p)
        if session_files:
            wait_end = clock.now().timestamp() + settings.get("reconcile_wait_s", DEFAULT_RECONCILE_WAIT_S)
            while True:
                rc = deps.reconcile(session_files, srv.db_path(run_dir))
                if not rc.get("missing_total") or clock.now().timestamp() >= wait_end:
                    break
                clock.sleep(POLL_S)
        sc = srv.condition()  # 中央ログの K_CENTRAL は試行の後に揃う
    except TrialAbort as e:
        abort = str(e).split(",")
    except Exception as e:  # noqa: BLE001  試行を無効にして次へ進む
        abort = [f"exception:{type(e).__name__}: {e}"[:200]]
    finally:
        if delay_applied:
            try:
                deps.delay.clear(trial.run_tag)
            except Exception as e:  # noqa: BLE001
                log(f"⚠ 遅延の解除に失敗: {e}")
        try:
            srv.stop()
        except Exception as e:  # noqa: BLE001
            log(f"⚠ サーバの停止に失敗: {e}")
            abort.append("server_stop_failed")
    if abort:
        verdict = Verdict(valid=False, reasons=abort)
    else:
        server = settings["server"]
        verdict = validate_trial(trial, events, ledgers, rc, sc, delay_ok=True if c.delay else None, terminals=tids,
                                 k_central=server["k_central"], edge_thresholds=server["edge_thresholds"])
    ctx = {**deps.context, "run_dir": str(run_dir) if run_dir else None, "server_condition": _jsonable(sc),
           "server_args": getattr(srv, "args", None),
           "net_delay": getattr(deps.delay, "last_output", None) if c.delay else None,
           "reconcile": {k: v for k, v in rc.items() if k != "terminals"}}
    timing = train_spans([e for e in events if e.get("type") == "session_complete"], raw_ledgers)
    m = mf.build_manifest(trial, attempt, verdict, ctx, events, started, clock.now(), timing=timing)
    m["reconcile"] = ctx["reconcile"]
    mf.write_manifest(Path(out_dir) / "manifests", m)
    log(f"  -> {'有効' if verdict.valid else '無効'} {verdict.reasons or ''} {verdict.marks or ''} (学習区間 {m['train_span_s']} s、ランナー {m['duration_s']} s)")
    return m


def _jsonable(sc: dict) -> dict:
    return {k: (sorted(v) if isinstance(v, set) else v) for k, v in (sc or {}).items()}


def run_all(trials: List[Trial], settings: dict, deps: Deps, out_dir: Path) -> List[dict]:
    out_dir = Path(out_dir)
    manifests: List[dict] = []
    valid: Dict[str, int] = {}
    durations: Dict[str, List[float]] = {}

    def attempt_trial(trial: Trial) -> None:
        for attempt in range(1, settings["max_retries"] + 2):
            m = run_one(trial, attempt, settings, deps, out_dir, _expected_s(trial, durations))
            manifests.append(m)
            deps.clock.sleep(settings["cooldown_s"])
            if m["valid"]:
                valid[trial.condition.name] = valid.get(trial.condition.name, 0) + 1
                durations.setdefault(trial.condition.name, []).append(m["duration_s"])
                return

    for t in trials:
        attempt_trial(t)

    conditions = list({t.condition.name: t.condition for t in trials}.values())
    seq = max((t.seq for t in trials), default=0)
    for r in range(settings["max_topup_rounds"]):
        short = [(c, c.n - valid.get(c.name, 0)) for c in conditions if valid.get(c.name, 0) < c.n]
        if not short:
            break
        deps.notifier.log(f"追加試行 {r + 1}: " + ", ".join(f"{c.name} ×{k}" for c, k in short))
        for c, k in short:
            for _ in range(k):
                seq += 1
                rep = sum(1 for m in manifests if m["condition"]["name"] == c.name and m["attempt"] == 1) + 1
                attempt_trial(Trial(seq=seq, block=-(r + 1), condition=c, rep=rep, run_tag=f"T{seq:03d}_{c.name}_{rep}"))

    summary = mf.summarize(manifests)
    path = out_dir / f"summary_{deps.clock.now():%Y%m%d_%H%M%S}.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(summary, encoding="utf-8")
    n_ok = sum(1 for m in manifests if m["valid"])
    deps.notifier.notify("HFL 無人試行 終了", f"有効 {n_ok} / 試行 {len(manifests)}（{path.name}）")
    return manifests
