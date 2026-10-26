# Serverside_HFL/tests/test_inference_inputs.py
import sqlite3

from central_server.endpoints import edge_update
import start


def test_latest_row_by_timestamp_not_id(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setattr(edge_update, "METRICS_DB_PATH", db)
    edge_update.init_metrics_db()
    edge_update.upsert_metric({"data_id": "new", "terminal_id": "t1", "timestamp_ms": 2000, "tp_measured_mbps": 9.0})
    edge_update.upsert_metric({"data_id": "old", "terminal_id": "t1", "timestamp_ms": 1000, "tp_measured_mbps": 1.0})
    edge_update.upsert_metric({"data_id": "x", "terminal_id": "t2", "timestamp_ms": 500, "virtual_router_id": "HFL_B5"})
    got = start._latest_rows_by_terminal(db)
    assert got["t1"]["data_id"] == "new" and got["t1"]["tp_measured_mbps"] == 9.0
    assert got["t2"]["virtual_router_id"] == "HFL_B5"


def test_current_ap():
    assert start._current_ap("HFL_A24") == "AP-A"
    assert start._current_ap("HFL_B5") == "AP-B"
    assert start._current_ap(None) == "?"


def test_inference_input_uses_app_needs_not_measured():
    apps = [{"appType": "ブラウザ", "needTP": 5, "needRTT": 0}, {"appType": "動画", "needTP": 10, "needRTT": 0},
            {"appType": "通話", "needTP": 0, "needRTT": 200}, {"appType": "配信", "needTP": 0, "needRTT": 100}]
    norm = {"tp_mean": 3.75, "tp_std": 2.5, "rtt_mean": 75.0, "rtt_std": 50.0}
    a = start._inference_input({"app_type": "配信", "tp_measured_mbps": 30.0, "rtt_measured_ms": 20.0}, apps, norm)
    b = start._inference_input({"app_type": "配信", "tp_measured_mbps": 1.0, "rtt_measured_ms": 900.0}, apps, norm)
    assert a == b == [(0 - 3.75) / 2.5, (100 - 75.0) / 50.0, 0.0, 0.0, 0.0, 1.0]   # 実測に依らない
    assert start._inference_input({"app_type": "unknown"}, apps, norm) is None
