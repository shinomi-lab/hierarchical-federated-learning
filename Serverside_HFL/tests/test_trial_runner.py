from pathlib import Path

import pytest

from scripts.trial_runner import adapters
from scripts.trial_runner.plan import DEFAULT_SERVER

RUN = Path("/runs/20261015_2200_T001_sync_none_1")


# ── Task 6: コマンド組み立て（実際の adb・サーバ・sudo は動かさない） ──

def test_adb_auto_start_cmd():
    assert adapters.adb_auto_start_cmd("192.168.11.11:5555", "T001_a_1", 5) == [
        "adb", "-s", "192.168.11.11:5555", "shell", "am", "start", "--activity-single-top", "-n",
        "com.example.hfl_experiment/.ui.MainActivity", "--ez", "auto_start", "true",
        "--es", "run_tag", "T001_a_1", "--ei", "num_cycles", "5"]
    assert "--ei" not in adapters.adb_auto_start_cmd("s", "T", None)


def test_server_start_cmd_async():
    env, argv = adapters.server_start_cmd(RUN, "async", DEFAULT_SERVER)
    assert env == {"HFL_MODE": "async"}
    assert argv[:2] == [str(RUN / "hfl"), "start.py"]
    s = " ".join(argv)
    assert "--run-mode production --env shinomilab --edges 2" in s
    assert "--hfl-mode async" in s and "--edge-thresholds 2,2 --k-central 1" in s
    assert "--central-threshold" not in s  # async の中央は K_CENTRAL だけを見る


def test_server_start_cmd_sync():
    env, argv = adapters.server_start_cmd(RUN, "sync", {**DEFAULT_SERVER, "env": "localhost"})
    s = " ".join(argv)
    assert env == {"HFL_MODE": "sync"} and "--env localhost" in s
    assert "--edge-thresholds 2,2 --central-threshold 2" in s and "--k-central" not in s


def test_parse_run_dir():
    out = "作成: /Users/x/hfl_runs/20261015_2200_T001_a_1（コミット abc123）\nサーバ起動（server ウィンドウで）:\n"
    assert adapters.parse_run_dir(out) == Path("/Users/x/hfl_runs/20261015_2200_T001_a_1")
    with pytest.raises(RuntimeError):
        adapters.parse_run_dir("既にある: /x")


def test_delay_apply_cmd():
    """非対称（A=100・B=0）は、A の範囲に遅延をかけ B を --exclude で除く。旧形式（--to B=0 --to A=100）は net_delay.py が拒否する
    （2026-10-22 の実機で、重なる指定は quick を付けても B にも遅延が漏れると判明）。B（NAT の WAN 側）は ping に応答しないので probe は A 側だけ。"""
    targets = {"A": "192.168.11.0/24", "B": "192.168.11.30"}
    probes = {"A": ["192.168.11.1"], "B": []}
    argv = adapters.delay_apply_cmd({"A": 100, "B": 0}, targets, probes, "T003_asym_1")
    s = " ".join(argv)
    assert argv[1].endswith("scripts/net_delay.py") and argv[2] == "apply"
    assert "--sudo-noninteractive" in argv
    assert "--to 192.168.11.0/24=100" in s and "--exclude 192.168.11.30" in s
    assert "--to 192.168.11.30" not in s
    assert "--probe 192.168.11.1" in s and "--note T003_asym_1" in s


def test_delay_apply_cmd_symmetric_has_no_exclude():
    targets = {"A": "192.168.11.0/24", "B": "192.168.11.30"}
    argv = adapters.delay_apply_cmd({"A": 100, "B": 100}, targets, {"A": ["192.168.11.1"], "B": []}, "T004_sym_1")
    s = " ".join(argv)
    assert "--to 192.168.11.0/24=100" in s and "--exclude" not in s


def test_delay_apply_cmd_rejects_b_only_delay():
    targets = {"A": "192.168.11.0/24", "B": "192.168.11.30"}
    with pytest.raises(ValueError):
        adapters.delay_apply_cmd({"A": 0, "B": 100}, targets, {"A": [], "B": []}, "x")


def test_delay_apply_cmd_is_accepted_by_net_delay_dry_run():
    """生成した引数を、実際の net_delay.py に --dry-run で通す（重なり・遅延違いの拒否に当たらないこと）。"""
    import subprocess, sys
    targets = {"A": "192.168.11.0/24", "B": "192.168.11.30"}
    argv = adapters.delay_apply_cmd({"A": 100, "B": 0}, targets, {"A": [], "B": []}, "x")
    r = subprocess.run(argv + ["--dry-run"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "to 192.168.11.30 " not in r.stdout


def test_central_k_values_from_start_py_log():
    log = ('[中央] 2026-10-15 22:00:01 - central_server - INFO - async_edge_received: {"edge_id": "edge-00", "K_CENTRAL": 1}\n'
           '[エッジ1] INFO: 127.0.0.1 - "GET /api/v1/meta HTTP/1.1" 200\n')
    assert adapters.central_k_values(log) == {1}


def test_edge_condition_from_meta():
    metas = {"edge-00": {"round": 1, "hfl_mode": "async", "aggregation_threshold": 2, "x": 1},
             "edge-01": {"hfl_mode": "async", "aggregation_threshold": "2"}}
    assert adapters.edge_condition(metas) == {"edge-00": {"hfl_mode": "async", "aggregation_threshold": 2},
                                              "edge-01": {"hfl_mode": "async", "aggregation_threshold": 2}}


# ── Task 7: 1 試行の流れ・再試行・追加試行（フェイク） ──

import json as _json
from datetime import datetime, timedelta

from scripts.trial_runner import plan, runner

TIDS = ["terminal-01", "terminal-02", "terminal-03", "terminal-04"]


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 10, 15, 22, 0, 0)

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


class FakeAdb:
    def __init__(self, server, unreachable=()):
        self.server, self.unreachable = server, set(unreachable)
        self.calls = []

    def ensure_connected(self, serial):
        return serial not in self.unreachable

    def restart_app(self, serial):
        self.calls.append(("restart", serial))

    def auto_start(self, serial, run_tag, num_cycles):
        self.calls.append(("auto_start", serial, run_tag))
        self.server.started(serial.replace("serial-", "terminal-"), run_tag, num_cycles)

    def pull_ledger(self, serial, dest):
        tid = serial.replace("serial-", "terminal-")
        dest.parent.mkdir(parents=True, exist_ok=True)
        lines = [{"data_id": "old", "run_id": "OLD", "terminal_id": tid}]  # 前のセッションの行（照合しない）
        for run_tag, n in self.server.sessions.get(tid, []):
            lines += [{"data_id": f"{run_tag}-{tid}-{c}", "run_id": f"R-{run_tag}-{tid}", "terminal_id": tid,
                       "cycle_number": c, "init_source": "DOWNLOADED", "round": c, "baseline_mode": "none",
                       "timestamp_ms": c * 10_000}
                      for c in range(1, n + 1)]
        dest.write_text("\n".join(_json.dumps(x) for x in lines) + "\n")
        return dest


class FakeServer:
    """drop: {attempt番号: 完了イベントを返さない端末の集合}（全体の prepare 回数で数える）。"""

    def __init__(self, drop=None, mode_override=None, fail_on_events=False):
        self.drop, self.mode_override, self.fail_on_events = drop or {}, mode_override, fail_on_events
        self.n_prepare = self.n_start = self.n_stop = 0
        self.sessions, self.done, self.mode = {}, [], None

    def prepare(self, name):
        self.n_prepare += 1
        self.done = []
        return Path(f"/runs/{name}")

    def start(self, run_dir, mode, server):
        self.n_start += 1
        self.mode = mode

    def stop(self):
        self.n_stop += 1

    def healthy(self):
        return True

    def set_control(self, baseline, num_cycles):
        pass

    def started(self, tid, run_tag, n):
        self.sessions.setdefault(tid, []).append((run_tag, n))
        if tid not in self.drop.get(self.n_prepare, set()):
            self.done.append({"type": "session_complete", "terminal_id": tid, "run_tag": run_tag, "result": "ok",
                              "run_id": f"R-{run_tag}-{tid}", "hfl_mode": self.mode, "num_cycles": n,
                              "after_switch_confirmed": "true", "started_at_ms": 0})

    def events(self, type_, run_tag=None):
        if self.fail_on_events:
            raise RuntimeError("boom")
        return [e for e in self.done if e["type"] == type_ and (run_tag is None or e["run_tag"] == run_tag)]

    def condition(self):
        m = self.mode_override or self.mode
        return {"edges": {e: {"hfl_mode": m, "aggregation_threshold": 2} for e in ("edge-00", "edge-01")},
                "k_central_seen": {1} if self.mode == "async" else set()}

    @staticmethod
    def db_path(run_dir):
        return Path(run_dir) / "db"


class FakeDelay:
    def __init__(self, ok=True):
        self.ok, self.applied, self.cleared = ok, 0, 0
        self.last_output = ""

    def apply(self, delay, run_tag):
        self.applied += 1
        return self.ok

    def clear(self, run_tag):
        self.cleared += 1


class FakeNotifier:
    def __init__(self):
        self.msgs = []

    def log(self, m):
        self.msgs.append(m)

    def notify(self, t, m):
        self.msgs.append(f"{t}: {m}")


def _settings(**kw):
    s = {**plan.DEFAULT_SETTINGS, "terminals": TIDS, "devices": {t: t.replace("terminal-", "serial-") for t in TIDS},
         "cooldown_s": 0, "reconcile_wait_s": 0}
    s.update(kw)
    return s


def _cond(name, mode, n=1, delay=None):
    return plan.Condition(name=name, mode=mode, baseline="none", num_cycles=2, delay=delay, n=n)


def _deps(server, adb=None, delay=None, reconciled=None):
    seen = reconciled if reconciled is not None else []

    def fake_reconcile(paths, db):
        seen.append([Path(p).read_text() for p in paths])
        return {"missing_total": 0}

    return runner.Deps(adb=adb or FakeAdb(server), server=server, delay=delay or FakeDelay(),
                       notifier=FakeNotifier(), clock=FakeClock(), reconcile=fake_reconcile,
                       context={"git_commit": "abc", "seed": 1})


def test_two_conditions_restart_server_every_trial(tmp_path):
    srv = FakeServer()
    deps = _deps(srv)
    trials = plan.order_trials([_cond("s", "sync"), _cond("a", "async")], seed=1)
    ms = runner.run_all(trials, _settings(), deps, tmp_path)
    assert [m["valid"] for m in ms] == [True, True], [m["reasons"] for m in ms]
    assert (srv.n_prepare, srv.n_start, srv.n_stop) == (2, 2, 2)
    assert sum(1 for c in deps.adb.calls if c[0] == "auto_start") == 8
    assert not hasattr(srv, "graceful_reset")
    assert len(list(tmp_path.glob("manifests/*.json"))) == 2 and list(tmp_path.glob("summary_*.md"))


def test_reconcile_only_this_session(tmp_path):
    srv, seen = FakeServer(), []
    runner.run_all(plan.order_trials([_cond("s", "sync")], seed=1), _settings(), _deps(srv, reconciled=seen), tmp_path)
    assert seen and all('"OLD"' not in text for text in seen[0])


def test_retry_after_incomplete(tmp_path):
    srv = FakeServer(drop={1: {"terminal-04"}})
    ms = runner.run_all(plan.order_trials([_cond("s", "sync")], seed=1), _settings(), _deps(srv), tmp_path)
    assert [(m["attempt"], m["valid"]) for m in ms] == [(1, False), (2, True)]
    assert "incomplete:terminal-04" in ms[0]["reasons"]


def test_give_up_and_topup_once(tmp_path):
    srv = FakeServer(drop={k: {"terminal-01"} for k in range(1, 20)})
    ms = runner.run_all(plan.order_trials([_cond("s", "sync")], seed=1), _settings(max_retries=2, max_topup_rounds=1),
                        _deps(srv), tmp_path)
    assert len(ms) == 6 and not any(m["valid"] for m in ms)  # 本番 3 回 + 追加 1 試行 × 3 回
    assert len({m["run_tag"] for m in ms}) == 2


def test_delay_cleared_and_server_stopped_on_exception(tmp_path):
    srv, d = FakeServer(fail_on_events=True), FakeDelay()
    ms = runner.run_all(plan.order_trials([_cond("d", "async", delay={"A": 100, "B": 0})], seed=1),
                        _settings(max_retries=0, max_topup_rounds=0), _deps(srv, delay=d), tmp_path)
    assert d.applied == d.cleared == 1 and srv.n_stop == 1
    assert not ms[0]["valid"] and ms[0]["reasons"][0].startswith("exception:")


def test_delay_verify_failed_skips_start(tmp_path):
    srv, d = FakeServer(), FakeDelay(ok=False)
    deps = _deps(srv, delay=d)
    ms = runner.run_all(plan.order_trials([_cond("d", "async", delay={"A": 100, "B": 0})], seed=1),
                        _settings(max_retries=0, max_topup_rounds=0), deps, tmp_path)
    assert ms[0]["reasons"] == ["delay_verify_failed"] and d.cleared == 1
    assert not [c for c in deps.adb.calls if c[0] == "auto_start"]


def test_server_condition_mismatch_invalid(tmp_path):
    srv = FakeServer(mode_override="sync")
    ms = runner.run_all(plan.order_trials([_cond("a", "async")], seed=1), _settings(max_retries=0, max_topup_rounds=0),
                        _deps(srv), tmp_path)
    assert "server_condition:hfl_mode" in ms[0]["reasons"]


def test_adb_unreachable(tmp_path):
    srv = FakeServer()
    deps = _deps(srv, adb=FakeAdb(srv, unreachable={"serial-03"}))
    ms = runner.run_all(plan.order_trials([_cond("s", "sync")], seed=1), _settings(max_retries=0, max_topup_rounds=0),
                        deps, tmp_path)
    assert ms[0]["reasons"] == ["adb_unreachable:terminal-03"] and srv.n_stop == 1


# ── Task 8: CLI ──

from scripts import run_trials

YAML = """settings:
  seed: 3
  terminals: [device-001, device-002]
  devices: {device-001: "192.168.11.11:5555", device-002: "192.168.11.12:5555"}
conditions:
  - {name: sync_none,  mode: sync,  baseline: none, num_cycles: 5, n: 2}
  - {name: async_none, mode: async, baseline: none, num_cycles: 5, n: 2}
"""


def test_cli_dry_run(tmp_path, capsys):
    p = tmp_path / "t.yaml"
    p.write_text(YAML, encoding="utf-8")
    assert run_trials.main([str(p), "--dry-run"], delay_available=lambda: False) == 0
    out = capsys.readouterr().out
    assert "T001_" in out and "T004_" in out and "4 試行" in out
    assert "--k-central 1" in out and "--central-threshold 2" in out


def test_cli_rejects_missing_device(tmp_path, capsys):
    p = tmp_path / "t.yaml"
    p.write_text(YAML.replace(', device-002: "192.168.11.12:5555"', ""), encoding="utf-8")
    assert run_trials.main([str(p), "--dry-run"], delay_available=lambda: False) == 2
    assert "device-002" in capsys.readouterr().err


def test_remaining_after_resume():
    import scripts.trial_runner.plan as pl
    cs = [pl.Condition("s", "sync", "none", 5, None, 3), pl.Condition("a", "async", "none", 5, None, 2)]
    ts = pl.order_trials(cs, seed=1)
    done = [{"condition": {"name": "s"}, "valid": True}, {"condition": {"name": "s"}, "valid": False},
            {"condition": {"name": "a"}, "valid": True}, {"condition": {"name": "a"}, "valid": True}]
    rest = run_trials.remaining(ts, done)
    assert sorted(t.condition.name for t in rest) == ["s", "s"]


def test_delay_apply_cmd_symmetric_adds_b_when_outside_a_range():
    """対称で B が A の範囲の外にあるとき、B にも同じ遅延を掛ける（レビュー指摘: B に掛からず非対称になる）。"""
    targets = {"A": "192.168.11.0/24", "B": "192.168.12.0/24"}
    s = " ".join(adapters.delay_apply_cmd({"A": 100, "B": 100}, targets, {"A": [], "B": []}, "x"))
    assert "--to 192.168.11.0/24=100" in s and "--to 192.168.12.0/24=100" in s


def test_delay_apply_cmd_symmetric_inside_a_range_has_single_rule():
    targets = {"A": "192.168.11.0/24", "B": "192.168.11.30"}
    s = " ".join(adapters.delay_apply_cmd({"A": 100, "B": 100}, targets, {"A": [], "B": []}, "x"))
    assert s.count("--to ") == 1 and "--exclude" not in s


def test_manifest_has_train_span_and_summary_columns(tmp_path):
    srv = FakeServer()
    ms = runner.run_all(plan.order_trials([_cond("s", "sync")], seed=1), _settings(), _deps(srv), tmp_path)
    m = ms[0]
    n = m["condition"]["num_cycles"]
    assert m["train_span_s"] == n * 10.0 and m["train_span_missing"] == []
    assert set(m["train_span_per_terminal_s"].values()) == {n * 10.0}
    md = next(tmp_path.glob("summary_*.md")).read_text(encoding="utf-8")
    assert "学習区間の平均 [s]" in md and "ランナーの所要時間の平均 [s]（参考）" in md


def test_poll_interval_is_short():
    assert runner.POLL_S <= 2
