"""エッジの任意ルーターの読み込み失敗の可視化（OpenSpec fix-ap-states-py39, edge-router-load-visibility）。

読み込みの失敗は、`sys.modules[<モジュール名>] = None` で再現する（import が ImportError になる）。
production の判定は環境変数 HFL_RUN_MODE（start.py がエッジに渡す）。未設定は production とみなさない。
"""
import logging
import sys

import pytest
from fastapi.testclient import TestClient

REQUIRED = ["ap_states", "terminal_telemetry"]
OPTIONAL_ONLY = ["network_sim", "virtual_congestion", "aggregation_control"]


@pytest.fixture()
def break_router(monkeypatch):
    def _break(name):
        monkeypatch.setitem(sys.modules, f"edge_server.endpoints.{name}", None)
    return _break


def _create_app():
    from edge_server.main import create_app
    return create_app()


def _healthz(app):
    r = TestClient(app).get("/healthz")
    assert r.status_code == 200
    return r.json()


def test_healthz_has_empty_failure_list_when_all_routers_load(monkeypatch):
    monkeypatch.delenv("HFL_RUN_MODE", raising=False)
    body = _healthz(_create_app())
    assert body["ok"] is True
    assert body["router_load_failures"] == []


@pytest.mark.parametrize("name", REQUIRED + OPTIONAL_ONLY)
def test_debug_failure_is_listed_and_logged_as_error(monkeypatch, break_router, caplog, name):
    monkeypatch.setenv("HFL_RUN_MODE", "debug")
    break_router(name)
    with caplog.at_level(logging.ERROR):
        app = _create_app()
    body = _healthz(app)
    assert body["ok"] is True                      # debug では起動を続け、ヘルスチェックは通る
    assert name in body["router_load_failures"]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and name in r.getMessage()]
    assert errors, f"{name} の読み込み失敗が ERROR で記録されていない"
    assert errors[0].exc_info, "スタックトレース（exc_info）が付いていない"


def test_unset_run_mode_is_not_production(monkeypatch, break_router):
    monkeypatch.delenv("HFL_RUN_MODE", raising=False)
    break_router("ap_states")
    assert "ap_states" in _healthz(_create_app())["router_load_failures"]  # 例外で止まらない


@pytest.mark.parametrize("name", REQUIRED)
def test_production_required_router_failure_stops_startup(monkeypatch, break_router, name):
    monkeypatch.setenv("HFL_RUN_MODE", "production")
    break_router(name)
    with pytest.raises(RuntimeError) as exc:
        _create_app()
    assert name in str(exc.value)


@pytest.mark.parametrize("name", OPTIONAL_ONLY)
def test_production_optional_router_failure_continues_and_is_listed(monkeypatch, break_router, name):
    monkeypatch.setenv("HFL_RUN_MODE", "production")
    break_router(name)
    body = _healthz(_create_app())
    assert name in body["router_load_failures"]
