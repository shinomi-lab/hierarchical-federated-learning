# Serverside_HFL/tests/test_compare_central_vs_hfl.py
import json
import subprocess
import sys
from pathlib import Path

import pytest

from central_server.endpoints import edge_update
from scripts.compare_central_vs_hfl import compare, render_markdown

ROOT = Path(__file__).resolve().parents[1]
BASE = 1_000_000


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "m.db"
    monkeypatch.setattr(edge_update, "METRICS_DB_PATH", p)
    edge_update.init_metrics_db()
    return p


def _cycle(i, run, term, cycle, acc, ts, tp=None, unit="Mbps", rnd=None):
    row = {"data_id": f"{run}-{term}-{cycle}-{i}", "run_id": run, "terminal_id": term, "cycle_number": cycle,
           "accuracy": acc, "timestamp_ms": ts, "round": rnd if rnd is not None else cycle}
    if tp is not None:
        row["tp_measured_mbps"] = tp
        if unit:
            row["tp_unit"] = unit
    edge_update.upsert_metric(row)


def _hfl_run(run="r1", tps=(8.0, 16.0), unit="Mbps"):
    """2 端末 × 3 サイクル。サイクル平均の精度は 0.61 / 0.71 / 0.81、サイクル末の時刻は +10/20/30 秒。"""
    for c, (a1, a2) in enumerate([(0.60, 0.62), (0.70, 0.72), (0.80, 0.82)], start=1):
        _cycle(1, run, "t1", c, a1, BASE + c * 10_000, tp=tps[0], unit=unit)
        _cycle(2, run, "t2", c, a2, BASE + c * 10_000 + 500, tp=tps[1], unit=unit)
    # 要約行（学習していないベースラインのセッション）は精度の比較に使わない
    edge_update.upsert_metric({"data_id": f"{run}-bs", "run_id": run, "terminal_id": "t1", "timestamp_ms": BASE + 99_000,
                               "row_kind": "baseline_session", "satisfaction_baseline_mean": 0.5})


META = {"val_accuracy": 0.78, "test_accuracy": 0.79, "train_seconds": 4.0, "rows": 8752, "epochs": 25, "seed": 42,
        "model_sha256": "abc"}


def test_reach_time_and_central_estimate(db):
    _hfl_run()
    rows = compare(db, META, ["r1"], data_bytes=8_000_000)
    assert len(rows) == 1
    r = rows[0]
    assert r["run_id"] == "r1" and r["n_terminals"] == 2
    assert r["target_accuracy"] == pytest.approx(0.78)
    assert r["hfl_reached_cycle"] == 3 and r["hfl_reached_round"] == 3
    assert r["hfl_elapsed_s"] == pytest.approx(20.5)   # 最初の行(+10.0 s)からサイクル 3 の最後の行(+30.5 s)まで
    assert r["hfl_final_accuracy"] == pytest.approx(0.81)
    assert r["central_val_accuracy"] == pytest.approx(0.78)
    assert r["central_train_s"] == pytest.approx(4.0)
    # 1 端末 4 MB = 32 Mbit。8 Mbps で 4 s、16 Mbps で 2 s。並列に送るので遅い方 = 4 s
    assert r["central_collect_est_s"] == pytest.approx(4.0)
    assert r["central_total_est_s"] == pytest.approx(8.0)


def test_target_not_reached_is_none(db):
    _hfl_run()
    r = compare(db, META, ["r1"], data_bytes=8_000_000, target=0.99)[0]
    assert r["target_accuracy"] == pytest.approx(0.99)
    assert r["hfl_reached_cycle"] is None and r["hfl_elapsed_s"] is None
    assert r["hfl_final_accuracy"] == pytest.approx(0.81)


def test_old_throughput_unit_makes_collect_estimate_unavailable(db):
    _hfl_run(unit=None)   # tp_unit が無い行は MB/s を Mbps としていた旧単位（CLAUDE.md E7）
    r = compare(db, META, ["r1"], data_bytes=8_000_000)[0]
    assert r["central_collect_est_s"] is None and r["central_total_est_s"] is None
    assert "tp_unit" in r["collect_note"]
    assert r["central_train_s"] == pytest.approx(4.0)   # 学習時間は推定に依らず出る


def test_no_data_bytes_means_no_collect_estimate(db):
    _hfl_run()
    r = compare(db, META, ["r1"], data_bytes=None)[0]
    assert r["central_collect_est_s"] is None and "data_bytes" in r["collect_note"]


def test_runs_default_to_all_runs_with_cycle_rows(db):
    _hfl_run("r1")
    _hfl_run("r2")
    edge_update.upsert_metric({"data_id": "only-bs", "run_id": "r3", "terminal_id": "t1", "timestamp_ms": BASE,
                               "row_kind": "baseline_session"})
    assert [r["run_id"] for r in compare(db, META, None, data_bytes=8_000_000)] == ["r1", "r2"]


def test_markdown_marks_estimates_and_conditions(db):
    _hfl_run()
    md = render_markdown(compare(db, META, ["r1"], data_bytes=8_000_000), META)
    assert "| r1 |" in md and "推定" in md
    assert "検証" in md   # 検証集合が異なることの注記
    assert "0.78" in md


def test_cli_prints_table(db, tmp_path):
    _hfl_run()
    meta_path = tmp_path / "central_model.meta.json"
    meta_path.write_text(json.dumps(META), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "scripts/compare_central_vs_hfl.py", "--db", str(db), "--central-meta", str(meta_path),
         "--data-bytes", "8000000", "--run-id", "r1"],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert "r1" in out and "推定" in out
