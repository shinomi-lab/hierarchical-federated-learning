import json
import sqlite3

import pytest

from central_server.endpoints import edge_update
from scripts.plot_satisfaction_timeline import load_rows, main, timeline_points


def test_timeline_orders_before_cycles_after():
    rows = [
        {"cycle_number": 2, "satisfaction_before": 0.4, "satisfaction_cycle": 0.55,
         "satisfaction_after": 0.6, "satisfaction_after_samples": [0.5, None, 0.7]},
        {"cycle_number": 1, "satisfaction_before": 0.4, "satisfaction_cycle": 0.5},
    ]
    pts = timeline_points(rows)
    assert [p["kind"] for p in pts] == ["before", "cycle", "cycle", "after", "after"]
    assert [p["value"] for p in pts] == [0.4, 0.5, 0.55, 0.5, 0.7]
    assert [p["x"] for p in pts] == [0, 1, 2, 3, 5]          # null の回は点を打たないが位置は保つ
    assert [p.get("sample") for p in pts[-2:]] == [1, 3]


def test_legacy_after_single_value():
    pts = timeline_points([{"cycle_number": 1, "satisfaction_before": 0.4, "satisfaction_cycle": 0.5, "satisfaction_after": 0.6}])
    assert pts[-1] == {"x": 2, "kind": "after", "sample": 1, "value": 0.6}


def test_baseline_session_row():
    row = {"row_kind": "baseline_session", "num_cycles": 2, "satisfaction_before": 0.3,
           "satisfaction_baseline_samples": [0.4, None] + [0.5] * 8,
           "satisfaction_after": 0.6, "satisfaction_after_samples": [0.6, 0.6]}
    pts = timeline_points([row])
    kinds = [p["kind"] for p in pts]
    assert kinds[0] == "before" and kinds.count("baseline") == 9 and kinds[-2:] == ["after", "after"]
    base = [p for p in pts if p["kind"] == "baseline"]
    assert base[0]["x"] == pytest.approx(0.2) and base[-1]["x"] == pytest.approx(2.0)   # 5 回 = 1 サイクル
    assert [p["x"] for p in pts if p["kind"] == "after"] == [3, 4]


def test_missing_values_are_skipped():
    pts = timeline_points([{"cycle_number": 1, "satisfaction_cycle": None}])
    assert pts == []


def test_load_rows_and_cli(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setattr(edge_update, "METRICS_DB_PATH", db)
    edge_update.init_metrics_db()
    for c in (1, 2):
        edge_update.upsert_metric({"data_id": f"d{c}", "terminal_id": "t1", "run_id": "r", "cycle_number": c,
                                   "satisfaction_before": 0.4, "satisfaction_cycle": 0.5 + c / 100})
    edge_update.upsert_metric({"data_id": "d2", "terminal_id": "t1", "satisfaction_after": 0.6,
                               "satisfaction_after_samples": [0.6, None]})
    edge_update.upsert_metric({"data_id": "x", "terminal_id": "t2", "run_id": "other", "cycle_number": 1})
    rows = load_rows(db, "r")
    assert set(rows) == {"t1"} and [r["cycle_number"] for r in rows["t1"]] == [1, 2]
    assert rows["t1"][1]["satisfaction_after_samples"] == [0.6, None]
    out = tmp_path / "fig.png"
    assert main(["--db", str(db), "--run-id", "r", "--out", str(out)]) == 0
    assert out.exists() and out.stat().st_size > 0
    assert main(["--db", str(db), "--run-id", "none", "--out", str(tmp_path / "n.png")]) == 1
