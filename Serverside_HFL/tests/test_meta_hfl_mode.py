# Serverside_HFL/tests/test_meta_hfl_mode.py
# SP3b R1: エッジの /api/v1/meta は自身の HFL_MODE を hfl_mode として返す（端末はこれを優先する）
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from edge_server.endpoints import terminal_update as tu


def _client():
    tu._control_fetched_at = float("inf")  # /meta からの中央取得を起こさない
    app = FastAPI()
    app.include_router(tu.router)
    return TestClient(app)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_meta_reports_hfl_mode(monkeypatch, mode):
    monkeypatch.setattr("edge_server.config.HFL_MODE", mode)
    monkeypatch.setattr(tu, "EXPERIMENT_CONTROL", {"baseline_mode": None, "overrides": {}, "version": 0})
    monkeypatch.setenv("EDGE_DISABLE_RATE_LIMITS", "1")
    r = _client().get("/api/v1/meta", headers={"X-Terminal-Id": "t1"})
    assert r.status_code == 200
    assert r.json()["hfl_mode"] == mode
