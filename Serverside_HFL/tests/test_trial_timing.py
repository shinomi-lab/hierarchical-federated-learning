from scripts.trial_runner.timing import train_spans

T = 1_000_000_000_000  # ms


def sc(tid, start_s, run_id=None):
    return {"type": "session_complete", "terminal_id": tid, "run_id": run_id or f"run-{tid}", "started_at_ms": T + start_s * 1000}


def cyc(tid, cycle, ts_s, after=False, run_id=None, data_id=None):
    return {"terminal_id": tid, "run_id": run_id or f"run-{tid}", "cycle_number": cycle, "data_id": data_id or f"{tid}-c{cycle}",
            "timestamp_ms": T + ts_s * 1000, "satisfaction_after": 0.9 if after else None}


def test_span_example_from_spec():
    starts = {"terminal-01": 0, "terminal-02": 5, "terminal-03": 6, "terminal-04": 8}
    ends = {"terminal-01": 100, "terminal-02": 140, "terminal-03": 120, "terminal-04": 130}
    events = [sc(t, s) for t, s in starts.items()]
    ledgers = {t: [cyc(t, 1, starts[t] + 10), cyc(t, 2, ends[t])] for t in starts}
    r = train_spans(events, ledgers)
    assert r["train_span_s"] == 140.0
    assert r["per_terminal_s"] == {"terminal-01": 100.0, "terminal-02": 135.0, "terminal-03": 114.0, "terminal-04": 122.0}
    assert r["missing"] == []


def test_after_completed_version_is_ignored():
    events = [sc("terminal-01", 0)]
    ledgers = {"terminal-01": [cyc("terminal-01", 5, 90), cyc("terminal-01", 5, 95), cyc("terminal-01", 5, 130, after=True)]}
    r = train_spans(events, ledgers)
    assert r["per_terminal_s"]["terminal-01"] == 95.0 and r["train_span_s"] == 95.0


def test_terminal_without_final_cycle_is_null_and_listed():
    events = [sc("terminal-01", 0), sc("terminal-02", 0)]
    ledgers = {"terminal-01": [cyc("terminal-01", 1, 50)], "terminal-02": []}
    r = train_spans(events, ledgers)
    assert r["per_terminal_s"]["terminal-02"] is None and r["missing"] == ["terminal-02"]
    assert r["train_span_s"] == 50.0


def test_other_run_ids_are_not_mixed():
    events = [sc("terminal-01", 0, run_id="A")]
    ledgers = {"terminal-01": [cyc("terminal-01", 5, 80, run_id="A"), cyc("terminal-01", 9, 500, run_id="B")]}
    assert train_spans(events, ledgers)["train_span_s"] == 80.0


def test_no_events_gives_none():
    r = train_spans([], {})
    assert r["train_span_s"] is None and r["per_terminal_s"] == {} and r["missing"] == []
