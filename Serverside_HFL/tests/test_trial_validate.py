from scripts.trial_runner import plan, validate

T = ["terminal-01", "terminal-02"]


def _trial(baseline="none", mode="sync", cycles=3):
    c = plan.Condition(name="c", mode=mode, baseline=baseline, num_cycles=cycles, delay=None, n=1)
    return plan.Trial(seq=1, block=0, condition=c, rep=1, run_tag="T001_c_1")


def _ok(cycles=3):
    ev = [{"type": "session_complete", "terminal_id": t, "run_tag": "T001_c_1", "result": "ok", "hfl_mode": "sync",
           "num_cycles": cycles, "after_switch_confirmed": "true", "started_at_ms": 1000} for t in T]
    led = {t: [{"cycle_number": i + 1, "init_source": "DOWNLOADED", "round": i + 1, "baseline_mode": "none"}
               for i in range(cycles)] for t in T}
    return ev, led, {"missing_total": 0}


SC = {"edges": {"edge-00": {"hfl_mode": "sync", "aggregation_threshold": 2},
                "edge-01": {"hfl_mode": "sync", "aggregation_threshold": 2}}, "k_central_seen": set()}


def test_ok():
    ev, led, rc = _ok()
    v = validate.validate_trial(_trial(), ev, led, rc, SC)
    assert v.valid and v.reasons == [] and v.marks == []


def test_missing_terminal_invalid():
    ev, led, rc = _ok()
    v = validate.validate_trial(_trial(), ev[:1], led, rc, SC, terminals=T)
    assert not v.valid and "incomplete:terminal-02" in v.reasons


def test_error_result_invalid():
    ev, led, rc = _ok()
    ev[0]["result"] = "error"
    assert "error:terminal-01" in validate.validate_trial(_trial(), ev, led, rc, SC).reasons


def test_reconcile_missing_invalid():
    ev, led, _ = _ok()
    assert "reconcile_missing:2" in validate.validate_trial(_trial(), ev, led, {"missing_total": 2}, SC).reasons


def test_row_count_and_init_source():
    ev, led, rc = _ok()
    led["terminal-01"] = led["terminal-01"][:2]
    led["terminal-02"][2]["init_source"] = "RANDOM"
    r = validate.validate_trial(_trial(), ev, led, rc, SC).reasons
    assert "rows:terminal-01=2" in r and "init_source:terminal-02:3" in r


def test_cycle1_must_be_downloaded():
    ev, led, rc = _ok()
    led["terminal-01"][0]["init_source"], led["terminal-01"][0]["round"] = "RANDOM", None
    r = validate.validate_trial(_trial(), ev, led, rc, SC).reasons
    assert "init_source:terminal-01:1" in r and "round_null:terminal-01:1" in r


def test_baseline_session_expects_summary_row():
    """ベースラインはサイクルの行を作らず、要約行（row_kind=baseline_session）を 1 行送る（SP3d 3.2）。"""
    ev, _, rc = _ok()
    led = {t: [{"row_kind": "baseline_session", "baseline_mode": "fixed"}] for t in T}
    assert validate.validate_trial(_trial(baseline="fixed"), ev, led, rc, SC).valid
    led["terminal-02"] = []
    assert "rows:terminal-02=0" in validate.validate_trial(_trial(baseline="fixed"), ev, led, rc, SC).reasons


def test_condition_mismatch():
    ev, led, rc = _ok()
    ev[0]["num_cycles"] = 5
    for row in led["terminal-02"]:
        row["baseline_mode"] = "fixed"
    r = validate.validate_trial(_trial(), ev, led, rc, SC).reasons
    assert "condition:num_cycles:terminal-01" in r and "condition:baseline_mode:terminal-02" in r


def test_server_condition_and_k_central():
    ev, led, rc = _ok()
    sc = {"edges": {"edge-00": {"hfl_mode": "async", "aggregation_threshold": 2},
                    "edge-01": {"hfl_mode": "async", "aggregation_threshold": 2}}, "k_central_seen": {1, 2}}
    for e in ev:
        e["hfl_mode"] = "async"
    r = validate.validate_trial(_trial(mode="async"), ev, led, rc, sc, k_central=1).reasons
    assert "server_condition:k_central" in r
    assert "server_condition:hfl_mode" in validate.validate_trial(_trial(mode="sync"), ev, led, rc, sc).reasons
    assert "server_condition:aggregation_threshold" in validate.validate_trial(
        _trial(mode="async"), ev, led, rc, sc, edge_thresholds=[3, 3]).reasons


def test_async_needs_central_log():
    ev, led, rc = _ok()
    sc = {"edges": {e: {"hfl_mode": "async", "aggregation_threshold": 2} for e in ("edge-00", "edge-01")},
          "k_central_seen": set()}
    for e in ev:
        e["hfl_mode"] = "async"
    assert "server_condition:k_central" in validate.validate_trial(_trial(mode="async"), ev, led, rc, sc, k_central=1).reasons


def test_delay_failed():
    ev, led, rc = _ok()
    assert "delay_verify_failed" in validate.validate_trial(_trial(), ev, led, rc, SC, delay_ok=False).reasons


def test_mismatch_after_and_skew_are_marks_only():
    ev, led, rc = _ok()
    ev.append({"type": "hfl_mode_mismatch", "terminal_id": "terminal-01", "app": "async", "server": "sync"})
    ev[1]["after_switch_confirmed"] = "false"
    ev[1]["started_at_ms"] = 1000 + 45_000
    v = validate.validate_trial(_trial(), ev, led, rc, SC)
    assert v.valid
    assert v.marks == ["after_unconfirmed:terminal-02", "hfl_mode_mismatch:terminal-01", "start_skew:45"]


def test_session_rows_dedupes_and_filters():
    lines = [
        {"data_id": "a", "run_id": "R", "cycle_number": 1, "satisfaction_after": None},
        {"data_id": "b", "run_id": "OLD", "cycle_number": 1},
        {"data_id": "a", "run_id": "R", "cycle_number": 1, "satisfaction_after": 0.8},  # 補完の追記
        {"data_id": "e", "run_id": "R", "kind": "experiment_event", "type": "session_complete"},
        {"data_id": "c", "run_id": "R", "cycle_number": 2},
    ]
    rows = validate.session_rows(lines, "R")
    assert [r["data_id"] for r in rows] == ["a", "c"] and rows[0]["satisfaction_after"] == 0.8


# --- Async では、新しいグローバルモデルがまだ無いとき端末は前サイクルの自モデルで続ける（init_source=PROVIDED）。2026-10-22 利用者了承 ---
def _async_ok(cycles=3):
    ev, led, rc = _ok(cycles)
    for e in ev:
        e["hfl_mode"] = "async"
    sc = {"edges": {"edge-00": {"hfl_mode": "async", "aggregation_threshold": 2},
                    "edge-01": {"hfl_mode": "async", "aggregation_threshold": 2}}, "k_central_seen": {1}}
    return ev, led, rc, sc


def test_async_allows_provided_after_cycle1_and_marks_it():
    ev, led, rc, sc = _async_ok(cycles=4)
    led["terminal-02"][1]["init_source"] = "PROVIDED"
    led["terminal-02"][2]["init_source"] = "PROVIDED"   # サイクル 4 は DOWNLOADED
    v = validate.validate_trial(_trial(mode="async", cycles=4), ev, led, rc, sc, k_central=1)
    assert v.valid and v.reasons == []
    assert "init_provided:terminal-02=2" in v.marks


def test_async_cycle1_must_still_be_downloaded():
    ev, led, rc, sc = _async_ok()
    led["terminal-01"][0]["init_source"] = "PROVIDED"
    v = validate.validate_trial(_trial(mode="async"), ev, led, rc, sc, k_central=1)
    assert "init_source:terminal-01:1" in v.reasons


def test_async_random_init_is_still_invalid():
    ev, led, rc, sc = _async_ok()
    led["terminal-01"][1]["init_source"] = "RANDOM"
    led["terminal-02"][2]["init_source"] = "RANDOM_LOAD_FAILED"
    r = validate.validate_trial(_trial(mode="async"), ev, led, rc, sc, k_central=1).reasons
    assert "init_source:terminal-01:2" in r and "init_source:terminal-02:3" in r


def test_sync_still_requires_downloaded_every_cycle():
    ev, led, rc = _ok()
    led["terminal-01"][1]["init_source"] = "PROVIDED"
    r = validate.validate_trial(_trial(mode="sync"), ev, led, rc, SC).reasons
    assert "init_source:terminal-01:2" in r


def test_async_needs_at_least_one_downloaded_after_cycle1():
    """全サイクルが PROVIDED（グローバルモデルが一度も届いていない）なら、Async でも無効（レビュー指摘）。"""
    ev, led, rc, sc = _async_ok(cycles=4)
    for i in (1, 2, 3):
        led["terminal-01"][i]["init_source"] = "PROVIDED"
    r = validate.validate_trial(_trial(mode="async", cycles=4), ev, led, rc, sc, k_central=1).reasons
    assert "init_never_downloaded:terminal-01" in r


def test_async_one_downloaded_after_cycle1_is_enough():
    ev, led, rc, sc = _async_ok(cycles=4)
    led["terminal-01"][1]["init_source"] = "PROVIDED"
    led["terminal-01"][2]["init_source"] = "PROVIDED"   # cycle 4 は DOWNLOADED のまま
    v = validate.validate_trial(_trial(mode="async", cycles=4), ev, led, rc, sc, k_central=1)
    assert v.valid
