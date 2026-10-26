# Serverside_HFL/tests/test_metrics_upsert.py
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from central_server.endpoints import edge_update


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "m.db"
    monkeypatch.setattr(edge_update, "METRICS_DB_PATH", p)
    edge_update.init_metrics_db()
    return p


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(edge_update.router)
    return TestClient(app)


def rows(db):
    with sqlite3.connect(str(db)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM metrics ORDER BY id")]


FIRST = {
    "data_id": "d-1", "terminal_id": "terminal-01", "edge_id": "edge-1", "round": 3,
    "accuracy": 0.61, "loss": 0.9, "run_id": "run-a", "cycle_number": 2, "epoch_count": 5,
    "val_accuracy_mean": 0.55, "app_type": "動画", "app_index": 1, "virtual_router_id": "HFL_A24",
    "timestamp_ms": 1000, "epoch_val_accuracy": [0.5, 0.61],
}


def test_same_data_id_twice_is_one_row_updated_by_non_null(client, db):
    assert client.post("/upload_training_metrics", json=FIRST).status_code == 200
    second = {"data_id": "d-1", "terminal_id": "terminal-01", "edge_id": "edge-1",
              "upload_success": True, "upload_duration_ms": 420, "tp_measured_mbps": 12.5,
              "rtt_measured_ms": 30.0, "satisfaction_cycle": 0.8, "accuracy": None, "timestamp_ms": 2000}
    assert client.post("/upload_training_metrics", json=second).status_code == 200
    r = rows(db)
    assert len(r) == 1
    assert r[0]["accuracy"] == pytest.approx(0.61)          # null は既存値を消さない
    assert r[0]["tp_measured_mbps"] == pytest.approx(12.5)
    assert r[0]["upload_success"] == 1
    assert r[0]["virtual_router_id"] == "HFL_A24"
    assert r[0]["timestamp_ms"] == 2000
    raw = json.loads(r[0]["raw_json"])
    assert raw["epoch_val_accuracy"] == [0.5, 0.61] and raw["upload_duration_ms"] == 420


def test_round_is_optional(client, db):
    body = {"data_id": "d-2", "terminal_id": "terminal-02", "edge_id": "edge-1"}
    assert client.post("/upload_training_metrics", json=body).status_code == 200
    assert rows(db)[0]["round"] is None


def test_missing_data_id_or_terminal_id_is_400(client, db):
    assert client.post("/upload_training_metrics", json={"terminal_id": "t"}).status_code == 400
    assert client.post("/upload_training_metrics", json={"data_id": "x"}).status_code == 400
    assert rows(db) == []


def test_legacy_form_body_is_stored(client, db):
    form = {"edge_id": "edge-1", "terminal_id": "terminal-03", "round": "4", "data_id": "d-3",
            "accuracy": "0.7", "app_type": "通話"}
    assert client.post("/upload_training_metrics", data=form).status_code == 200
    r = rows(db)[0]
    assert r["round"] == 4 and r["accuracy"] == pytest.approx(0.7) and r["app_type"] == "通話"


def test_legacy_not_null_schema_is_migrated(tmp_path, monkeypatch):
    p = tmp_path / "old.db"
    with sqlite3.connect(str(p)) as c:
        c.execute("""CREATE TABLE metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, edge_id TEXT NOT NULL,
                     round INTEGER NOT NULL, accuracy REAL, loss REAL, data_id TEXT NOT NULL, event_timestamp TEXT)""")
        c.execute("INSERT INTO metrics (edge_id, round, accuracy, data_id) VALUES ('e', 1, 0.5, 'old-1')")
    monkeypatch.setattr(edge_update, "METRICS_DB_PATH", p)
    edge_update.init_metrics_db()
    edge_update.upsert_metric({"data_id": "new-1", "terminal_id": "t"})
    with sqlite3.connect(str(p)) as c:
        got = c.execute("SELECT data_id, round FROM metrics ORDER BY id").fetchall()
    assert got == [("old-1", 1), ("new-1", None)]


def test_session_id_is_filled_from_central_logger(client, db, monkeypatch):
    # main 由来の session_id（graceful_reset でローテート）をアップサートでも保持する
    monkeypatch.setattr(edge_update.central_time_logger, "current_session", "20261013_120000", raising=False)
    client.post("/upload_training_metrics", json={"data_id": "s1", "terminal_id": "t"})
    assert rows(db)[0]["session_id"] == "20261013_120000"


# --- SP3d: サイクル数と After の標準偏差・件数 ---
def test_after_summary_columns_filled_by_completion(client, db):
    assert client.post("/upload_training_metrics", json=FIRST).status_code == 200
    after = {"data_id": "d-1", "terminal_id": "terminal-01", "satisfaction_after": 0.6,
             "satisfaction_after_samples": [0.5, None, 0.7], "satisfaction_after_std": 0.1414,
             "satisfaction_after_n": 2, "num_cycles": 5, "num_cycles_source": "server", "timestamp_ms": 3000}
    assert client.post("/upload_training_metrics", json=after).status_code == 200
    r = rows(db)
    assert len(r) == 1
    assert r[0]["satisfaction_after_n"] == 2 and r[0]["satisfaction_after_std"] == pytest.approx(0.1414)
    assert r[0]["num_cycles"] == 5 and r[0]["accuracy"] == pytest.approx(0.61)
    raw = json.loads(r[0]["raw_json"])
    assert raw["satisfaction_after_samples"] == [0.5, None, 0.7] and raw["num_cycles_source"] == "server"


def test_baseline_session_row_then_after_completion(client, db):
    """SP3d 3.2: ベースラインのセッション要約行（cycle_number 無し）にも After を同じ data_id で補完できる。"""
    summary = {"data_id": "bs-1", "terminal_id": "terminal-02", "edge_id": "edge-1", "run_id": "run-b",
               "row_kind": "baseline_session", "baseline_mode": "fixed", "baseline_mode_source": "server",
               "num_cycles": 3, "num_cycles_source": "server", "baseline_measurements": 15,
               "satisfaction_before": 0.4, "satisfaction_baseline_mean": 0.5, "satisfaction_baseline_n": 15,
               "satisfaction_baseline_samples": [0.5] * 15, "timestamp_ms": 1000}
    assert client.post("/upload_training_metrics", json=summary).status_code == 200
    after = dict(summary, satisfaction_after=0.6, satisfaction_after_samples=[0.5, 0.6, 0.7, 0.6, 0.6],
                 satisfaction_after_std=0.0707, satisfaction_after_n=5, timestamp_ms=2000)
    assert client.post("/upload_training_metrics", json=after).status_code == 200
    r = rows(db)
    assert len(r) == 1
    assert r[0]["cycle_number"] is None and r[0]["run_id"] == "run-b" and r[0]["num_cycles"] == 3
    assert r[0]["satisfaction_before"] == pytest.approx(0.4) and r[0]["satisfaction_after"] == pytest.approx(0.6)
    assert r[0]["satisfaction_after_n"] == 5
    raw = json.loads(r[0]["raw_json"])
    assert raw["row_kind"] == "baseline_session" and raw["baseline_mode"] == "fixed"
    assert len(raw["satisfaction_after_samples"]) == 5


def test_old_db_gets_new_columns(tmp_path, monkeypatch):
    p = tmp_path / "old.db"
    with sqlite3.connect(str(p)) as c:
        c.execute("CREATE TABLE metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, data_id TEXT, raw_json TEXT)")
    monkeypatch.setattr(edge_update, "METRICS_DB_PATH", p)
    edge_update.init_metrics_db()
    with sqlite3.connect(str(p)) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(metrics)")}
    assert {"num_cycles", "satisfaction_after_std", "satisfaction_after_n"} <= cols

