# Serverside_HFL/tests/test_reconcile_metrics.py
import json
import subprocess
import sys
from pathlib import Path

from central_server.endpoints import edge_update
from scripts.reconcile_metrics import reconcile

ROOT = Path(__file__).resolve().parents[1]


def _ledger(tmp_path, rows):
    p = tmp_path / "ledger.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def test_detects_missing_and_mismatch_using_last_ledger_line(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setattr(edge_update, "METRICS_DB_PATH", db)
    edge_update.init_metrics_db()
    edge_update.upsert_metric({"data_id": "a", "terminal_id": "t1", "accuracy": 0.5})
    edge_update.upsert_metric({"data_id": "b", "terminal_id": "t1", "accuracy": 0.9})
    led = _ledger(tmp_path, [
        {"data_id": "a", "terminal_id": "t1", "accuracy": 0.4},
        {"data_id": "a", "terminal_id": "t1", "accuracy": 0.5},   # 後の行が有効 → 一致
        {"data_id": "b", "terminal_id": "t1", "accuracy": 0.8},   # 不一致
        {"data_id": "c", "terminal_id": "t1", "accuracy": 0.1},   # 欠損
    ])
    r = reconcile([led], db)
    t = r["terminals"]["t1"]
    assert (t["ledger"], t["in_db"], t["missing"]) == (3, 2, ["c"])
    assert t["mismatched"] == [{"data_id": "b", "field": "accuracy", "ledger": 0.8, "db": 0.9}]
    assert r["missing_total"] == 1


def test_baseline_session_row_is_reconciled(tmp_path, monkeypatch):
    """SP3d 3.2: ベースラインの要約行（After 補完後）も data_id で照合され、欠損 0。"""
    db = tmp_path / "m.db"
    monkeypatch.setattr(edge_update, "METRICS_DB_PATH", db)
    edge_update.init_metrics_db()
    base = {"data_id": "bs", "terminal_id": "t2", "run_id": "r", "row_kind": "baseline_session",
            "baseline_mode": "fixed", "num_cycles": 3, "satisfaction_before": 0.4}
    after = dict(base, satisfaction_after=0.6, satisfaction_after_n=5, satisfaction_after_samples=[0.6] * 5)
    edge_update.upsert_metric(base)
    edge_update.upsert_metric(after)
    led = _ledger(tmp_path, [base, {"kind": "experiment_event", "data_id": "ev", "terminal_id": "t2", "run_id": "r"}, after])
    r = reconcile([led], db)
    assert r["missing_total"] == 0 and r["terminals"]["t2"]["mismatched"] == []


def test_cli_exit_code(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setattr(edge_update, "METRICS_DB_PATH", db)
    edge_update.init_metrics_db()
    edge_update.upsert_metric({"data_id": "a", "terminal_id": "t1"})
    ok = _ledger(tmp_path, [{"data_id": "a", "terminal_id": "t1"}])
    run = lambda *a: subprocess.run([sys.executable, "scripts/reconcile_metrics.py", "--db", str(db), *a],
                                    cwd=ROOT, capture_output=True, text=True)
    assert run(str(ok)).returncode == 0
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"data_id": "zz", "terminal_id": "t1"}) + "\n")
    assert run(str(bad)).returncode == 1


def test_experiment_event_rows_are_not_metrics(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setattr(edge_update, "METRICS_DB_PATH", db)
    edge_update.init_metrics_db()
    edge_update.upsert_metric({"data_id": "a", "terminal_id": "t1"})
    led = _ledger(tmp_path, [{"data_id": "a", "terminal_id": "t1"},
                             {"data_id": "ev1", "terminal_id": "t1", "kind": "experiment_event", "type": "baseline_mode_change"}])
    assert reconcile([led], db)["missing_total"] == 0
