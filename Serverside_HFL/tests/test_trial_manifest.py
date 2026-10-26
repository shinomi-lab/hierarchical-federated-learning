import json
from datetime import datetime

from scripts.trial_runner import manifest, plan, validate


def _trial(name="sync_none", seq=1):
    c = plan.Condition(name=name, mode="sync", baseline="none", num_cycles=5, delay=None, n=2)
    return plan.Trial(seq=seq, block=0, condition=c, rep=1, run_tag=f"T{seq:03d}_{name}_1")


CTX = {"git_commit": "abc123", "git_dirty": False, "apk_sha256": "f" * 64, "seed": 7, "trials_yaml_sha256": "e" * 64,
       "server_args": ["start.py", "--edges", "2"], "run_dir": "/runs/x", "server_condition": {}, "net_delay": None}
T0, T1 = datetime(2026, 10, 15, 22, 0, 0), datetime(2026, 10, 15, 22, 3, 30)


def _m(valid=True, reasons=(), name="sync_none", seq=1, attempt=1):
    v = validate.Verdict(valid=valid, reasons=list(reasons), marks=[])
    ev = [{"type": "session_complete", "terminal_id": "terminal-01", "result": "ok"}]
    return manifest.build_manifest(_trial(name, seq), attempt, v, CTX, ev, T0, T1)


def test_build_manifest_keys():
    m = _m()
    for k in ("run_tag", "condition", "attempt", "valid", "reasons", "marks", "git_commit", "apk_sha256", "seed",
              "session_complete", "started_at", "ended_at", "duration_s", "run_dir"):
        assert k in m, k
    assert m["condition"]["name"] == "sync_none" and m["duration_s"] == 210.0
    json.dumps(m)  # JSON にできる


def test_write_does_not_overwrite(tmp_path):
    p1 = manifest.write_manifest(tmp_path, _m())
    p2 = manifest.write_manifest(tmp_path, _m())
    assert p1 != p2 and p1.name == "20261015_220000_sync_none_001.json"
    assert json.loads(p1.read_text())["run_tag"] == "T001_sync_none_1"


def test_summarize():
    ms = [_m(), _m(valid=False, reasons=["reconcile_missing:2"], attempt=1, seq=2),
          _m(seq=2, attempt=2), _m(valid=False, reasons=["incomplete:terminal-03"], name="async_none", seq=3)]
    md = manifest.summarize(ms)
    assert "| sync_none | 2 | 3 |" in md
    assert "| async_none | 0 | 1 |" in md
    assert "reconcile_missing ×1" in md and "incomplete ×1" in md


def test_summarize_shows_train_span_first_and_runner_time_as_reference():
    v = validate.Verdict(valid=True, reasons=[], marks=[])
    ev = [{"type": "session_complete", "terminal_id": "terminal-01", "result": "ok"}]
    ms = [manifest.build_manifest(_trial(), 1, v, CTX, ev, T0, T1, timing={"train_span_s": s, "per_terminal_s": {}, "missing": []})
          for s in (100.0, 120.0)]
    md = manifest.summarize(ms)
    assert "| sync_none | 2 | 2 | 110.0 | 210 | - |" in md
    assert ms[0]["train_span_s"] == 100.0 and ms[0]["duration_s"] == 210.0


def test_manifest_without_timing_keeps_old_fields():
    m = _m()
    assert m["train_span_s"] is None and m["train_span_per_terminal_s"] == {} and m["train_span_missing"] == []
