import json

from scripts import trial_timing_report as rep


def _trial(d, name, mode, seq, run_tag, terms):
    m = {"run_tag": run_tag, "seq": seq, "attempt": 1, "valid": True, "condition": {"name": name, "mode": mode},
         "duration_s": 170.0, "session_complete": []}
    ldir = d / "ledgers" / f"{run_tag}_a1"
    ldir.mkdir(parents=True)
    for tid, (start, end) in terms.items():
        rid = f"{run_tag}-{tid}"
        m["session_complete"].append({"type": "session_complete", "terminal_id": tid, "run_id": rid, "started_at_ms": start * 1000})
        rows = [{"run_id": rid, "cycle_number": 5, "data_id": "x", "timestamp_ms": end * 1000, "satisfaction_after": None},
                {"run_id": rid, "cycle_number": 5, "data_id": "x", "timestamp_ms": (end + 30) * 1000, "satisfaction_after": 0.9}]
        (ldir / f"{tid}.raw.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "manifests").mkdir(exist_ok=True)
    (d / "manifests" / f"{seq:03d}_{name}.json").write_text(json.dumps(m))


def test_report_from_trials_dir(tmp_path, capsys):
    _trial(tmp_path, "sync_sym", "sync", 1, "T001_sync_sym_1", {"terminal-01": (0, 160), "terminal-02": (2, 150)})
    _trial(tmp_path, "async_sym", "async", 2, "T002_async_sym_1", {"terminal-01": (0, 130), "terminal-02": (3, 90)})
    rows = rep.collect(tmp_path)
    assert [(r["run_tag"], r["train_span_s"]) for r in rows] == [("T001_sync_sym_1", 160.0), ("T002_async_sym_1", 130.0)]
    assert rep.main(["--trials-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "sync_sym" in out and "160.0" in out and "比" in out and "1.23" in out   # 160/130


def test_invalid_trials_are_excluded_from_means(tmp_path):
    _trial(tmp_path, "sync_sym", "sync", 1, "T001_sync_sym_1", {"terminal-01": (0, 160)})
    p = next((tmp_path / "manifests").glob("*.json"))
    m = json.loads(p.read_text()); m["valid"] = False; p.write_text(json.dumps(m))
    means = rep.condition_means(rep.collect(tmp_path))
    assert means == {}
