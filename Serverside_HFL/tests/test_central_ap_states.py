"""central_server.endpoints.ap_states の試験（OpenSpec record-connected-ap 1.2, edge-ap-states-api）。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared.ap_state import ApStateAggregator, ap_ids_for


@pytest.fixture()
def api(monkeypatch):
    from central_server.endpoints import ap_states  # Python 3.9 で読み込めなければ、ここで失敗する

    monkeypatch.setattr(ap_states, "aggregator", ApStateAggregator(ap_ids=ap_ids_for(2)))
    app = FastAPI()
    app.include_router(ap_states.router)
    return TestClient(app)


def test_two_terminals_observe_then_get_median_and_count(api):
    api.post("/api/v1/ap_states/observe", json={"terminal_id": "terminal-01", "ap": "AP1", "tp_mbps": 6.0, "rtt_ms": 40.0})
    api.post("/api/v1/ap_states/observe", json={"terminal_id": "terminal-03", "ap": "AP1", "tp_mbps": 8.0, "rtt_ms": 20.0})
    r = api.get("/api/v1/ap_states")
    assert r.status_code == 200
    assert r.headers["X-Ap-States-Source"] == "central"
    b = r.json()["AP1"]
    assert b["n_connected"] == 2
    assert b["measured"]["tp_mbps"] == 7.0 and b["measured"]["rtt_ms"] == 30.0 and b["measured"]["n"] == 2
    assert b["ap_states_source"] == "central"


def test_top_level_keys_are_only_aps(api):
    """旧 APK のパーサは最上位の全キーを AP オブジェクトとして読む。AP 以外のキーを足してはならない。"""
    body = api.get("/api/v1/ap_states").json()
    assert set(body) == {"AP0", "AP1"}
    assert all(isinstance(v, dict) for v in body.values())
    for state in body.values():
        assert {"tp_mbps", "rtt_ms", "n_connected"} <= set(state)  # 旧 APK が読む項目は残っている


def test_connect_updates_terminal_ap_and_repeats_do_not_inflate(api):
    for _ in range(24):
        r = api.post("/api/v1/ap_states/connect", json={"terminal_id": "terminal-01", "from_ap": "AP1", "to_ap": "AP0"})
        assert r.status_code == 200
    assert r.json()["connected"] == {"AP0": 1, "AP1": 0}
    api.post("/api/v1/ap_states/connect", json={"terminal_id": "terminal-01", "to_ap": "AP1"})  # from_ap 省略（初回接続の形）
    assert api.get("/api/v1/ap_states").json()["AP1"]["n_connected"] == 1


@pytest.mark.parametrize("path,payload", [
    ("/api/v1/ap_states/connect", {"terminal_id": "t", "to_ap": "AP2"}),
    ("/api/v1/ap_states/connect", {"terminal_id": "t", "to_ap": "AP9"}),
    ("/api/v1/ap_states/observe", {"terminal_id": "t", "ap": "AP2", "tp_mbps": 1.0}),
])
def test_unknown_ap_is_400_and_counts_unchanged(api, path, payload):
    assert api.post(path, json=payload).status_code == 400
    assert all(s["n_connected"] == 0 for s in api.get("/api/v1/ap_states").json().values())


def test_registered_in_central_app():
    from central_server.main import create_app  # noqa: F401  — 登録されていれば import と include で落ちない
    app = create_app() if callable(create_app) else None
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/v1/ap_states" in paths and "/api/v1/ap_states/observe" in paths
