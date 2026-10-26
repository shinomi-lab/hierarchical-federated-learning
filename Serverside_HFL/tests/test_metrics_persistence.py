import sqlite3

import pytest

from central_server.endpoints import edge_update


def _insert_sample(db_path):
    """init + insert one metrics row carrying terminal_id and measured TP/RTT."""
    edge_update.init_metrics_db()
    edge_update._insert_metrics(
        edge_id="edge-server-02",
        round=0,
        accuracy=-1.0,
        loss=-1.0,
        data_id="06c1b07e-34d9-4232-979f-93bb52f1b257",
        event_timestamp="2026-07-09T21:09:13.064104",
        terminal_id="terminal-04",
        app_type="通話",
        app_index=2,
        satisfaction_before=1.0,
        satisfaction_after=1.0,
        tp_measured_mbps=2.4213075060532687,
        rtt_measured_ms=170.0,
    )
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM metrics ORDER BY id DESC LIMIT 1"
        ).fetchone()


def test_insert_metrics_persists_terminal_id(tmp_path, monkeypatch):
    db = tmp_path / "training_metrics.db"
    monkeypatch.setattr(edge_update, "METRICS_DB_PATH", db)
    row = _insert_sample(db)
    assert row["terminal_id"] == "terminal-04"


def test_insert_metrics_persists_measured_tp_rtt(tmp_path, monkeypatch):
    db = tmp_path / "training_metrics.db"
    monkeypatch.setattr(edge_update, "METRICS_DB_PATH", db)
    row = _insert_sample(db)
    assert row["tp_measured_mbps"] == pytest.approx(2.4213075060532687)
    assert row["rtt_measured_ms"] == pytest.approx(170.0)


def test_tests_never_point_at_real_experiment_db():
    # edge_update は import 時に init_metrics_db() を実行するため、
    # テストでは保存先が実験DB(Serverside_HFL/received_edges)であってはならない
    from pathlib import Path
    real = (Path(edge_update.__file__).resolve().parents[2] / "received_edges").resolve()
    assert edge_update.BASE_DIR != real
