# Serverside_HFL/tests/test_edge_experiment_control.py
from fastapi import FastAPI
from fastapi.testclient import TestClient

from edge_server.endpoints import terminal_update as tu


def _client():
    tu._control_fetched_at = float("inf")  # 既存テストでは /meta からの中央取得を起こさない
    app = FastAPI()
    app.include_router(tu.router)
    return TestClient(app)


def test_meta_resolves_per_terminal(monkeypatch):
    monkeypatch.setattr(tu, "EXPERIMENT_CONTROL", {"baseline_mode": "fixed", "overrides": {"t2": "random"}, "version": 3})
    monkeypatch.setenv("EDGE_DISABLE_RATE_LIMITS", "1")
    c = _client()
    assert c.get("/api/v1/meta", headers={"X-Terminal-Id": "t2"}).json()["baseline_mode"] == "random"
    m = c.get("/api/v1/meta", headers={"X-Terminal-Id": "t1"}).json()
    assert m["baseline_mode"] == "fixed" and m["experiment_control_version"] == 3


def test_meta_omits_mode_when_unset(monkeypatch):
    monkeypatch.setattr(tu, "EXPERIMENT_CONTROL", {"baseline_mode": None, "overrides": {}, "version": 0})
    monkeypatch.setenv("EDGE_DISABLE_RATE_LIMITS", "1")
    assert "baseline_mode" not in _client().get("/api/v1/meta", headers={"X-Terminal-Id": "t1"}).json()


def test_refresh_keeps_previous_on_failure(monkeypatch):
    monkeypatch.setattr(tu, "EXPERIMENT_CONTROL", {"baseline_mode": "fixed", "overrides": {}, "version": 1})
    def boom(url, timeout):
        raise RuntimeError("down")
    tu.refresh_experiment_control(fetch=boom)
    assert tu.EXPERIMENT_CONTROL["baseline_mode"] == "fixed"
    tu.refresh_experiment_control(fetch=lambda url, timeout: {"baseline_mode": "greedy", "overrides": {}, "version": 2})
    assert tu.EXPERIMENT_CONTROL["baseline_mode"] == "greedy"


# --- SP3d: サイクル数 ---
def test_meta_returns_num_cycles_per_terminal(monkeypatch):
    monkeypatch.setattr(tu, "EXPERIMENT_CONTROL", {"baseline_mode": None, "overrides": {}, "num_cycles": 20,
                                                   "num_cycles_overrides": {"t2": 3}, "version": 5})
    monkeypatch.setenv("EDGE_DISABLE_RATE_LIMITS", "1")
    c = _client()
    assert c.get("/api/v1/meta", headers={"X-Terminal-Id": "t2"}).json()["num_cycles"] == 3
    m = c.get("/api/v1/meta", headers={"X-Terminal-Id": "t1"}).json()
    assert m["num_cycles"] == 20 and "baseline_mode" not in m


def test_meta_omits_num_cycles_when_unset(monkeypatch):
    monkeypatch.setattr(tu, "EXPERIMENT_CONTROL", {"baseline_mode": None, "overrides": {}, "version": 0})  # 旧形式
    monkeypatch.setenv("EDGE_DISABLE_RATE_LIMITS", "1")
    assert "num_cycles" not in _client().get("/api/v1/meta", headers={"X-Terminal-Id": "t1"}).json()


def test_resolve_control_for_terminal():
    ctl = {"baseline_mode": "fixed", "overrides": {"t2": "random"}, "num_cycles": 20, "num_cycles_overrides": {"t3": 2}}
    assert tu.resolve_control_for_terminal(ctl, "t2") == {"baseline_mode": "random", "num_cycles": 20}
    assert tu.resolve_control_for_terminal(ctl, "t3") == {"baseline_mode": "fixed", "num_cycles": 2}
    assert tu.resolve_control_for_terminal({"overrides": {}}, None) == {}


def test_refresh_copies_num_cycles(monkeypatch):
    monkeypatch.setattr(tu, "EXPERIMENT_CONTROL", {"baseline_mode": None, "overrides": {}, "version": 0})
    tu.refresh_experiment_control(fetch=lambda url, timeout: {"baseline_mode": None, "overrides": {}, "num_cycles": 7,
                                                              "num_cycles_overrides": {"t3": 2}, "version": 6})
    assert tu.EXPERIMENT_CONTROL["num_cycles"] == 7 and tu.EXPERIMENT_CONTROL["num_cycles_overrides"] == {"t3": 2}


def test_event_forwarding(monkeypatch):
    sent = {}
    async def fake(path, body, transport=None):
        sent["path"], sent["body"] = path, body
        return 200, "ok"
    monkeypatch.setattr(tu, "forward_json_to_central", fake)
    r = _client().post("/api/v1/experiment_events", json={"type": "baseline_mode_change", "to": "fixed"})
    assert r.status_code == 200 and sent["path"] == "api/v1/experiment_events" and sent["body"]["to"] == "fixed"
    async def down(path, body, transport=None):
        return 0, "x"
    monkeypatch.setattr(tu, "forward_json_to_central", down)
    assert _client().post("/api/v1/experiment_events", json={"type": "x"}).status_code == 502


def test_meta_refreshes_control_when_stale(monkeypatch):
    # エッジの中央ポーラーは起動されていないため、/meta 要求時に TTL 切れなら中央から取り直す
    calls = []
    def fake_refresh(fetch=None):
        calls.append(1)
        tu.EXPERIMENT_CONTROL = {"baseline_mode": "greedy", "overrides": {}, "version": 9}
    monkeypatch.setattr(tu, "refresh_experiment_control", fake_refresh)
    monkeypatch.setenv("EDGE_DISABLE_RATE_LIMITS", "1")
    app = FastAPI(); app.include_router(tu.router); c = TestClient(app)
    tu._control_fetched_at = 0.0
    assert c.get("/api/v1/meta", headers={"X-Terminal-Id": "t1"}).json()["baseline_mode"] == "greedy"
    c.get("/api/v1/meta", headers={"X-Terminal-Id": "t1"})   # TTL 内は取り直さない
    assert len(calls) == 1


def test_central_mode_passes_through_edge_meta(monkeypatch):
    """central は端末の実験モード。エッジは不正として落とさず、解決した値をそのまま端末に返す。"""
    ctl = {"baseline_mode": "fixed", "overrides": {"t2": "central"}, "num_cycles": None, "num_cycles_overrides": {}}
    assert tu.resolve_control_for_terminal(ctl, "t2") == {"baseline_mode": "central"}
    assert tu.resolve_control_for_terminal(ctl, "t1") == {"baseline_mode": "fixed"}
