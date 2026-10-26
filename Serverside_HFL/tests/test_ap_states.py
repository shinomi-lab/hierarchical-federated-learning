"""edge_server.endpoints.ap_states の試験（OpenSpec fix-ap-states-py39 → record-connected-ap 1.3, edge-ap-states-api）。

2026-10-21 のラボで、サーバの Python 3.9 では `from_ap: str | None`（3.10 以降の構文）のために
このルーターが読み込まれず、main.py の握りつぶしで気づかれなかった。ここでは import をフィクスチャの中で行い、
読み込みの失敗が「試験の失敗」として見えるようにしている。

record-connected-ap で、エッジは観測・接続通知を中央へ転送し、GET は中央の集計を返す（中央に届かなければ、そのエッジが
受けた分だけの代替）。中央の代わりに、本物の中央ルーターを ASGI で繋いだ偽の HTTP クライアントを使う。
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared.ap_state import ApStateAggregator, ap_ids_for


class FakeCentral:
    """本物の central_server.endpoints.ap_states を、ネットワーク無しで繋ぐ。"""

    def __init__(self, monkeypatch):
        from central_server.endpoints import ap_states as central

        self.agg = ApStateAggregator(ap_ids=ap_ids_for(2))
        monkeypatch.setattr(central, "aggregator", self.agg)
        app = FastAPI()
        app.include_router(central.router)
        self.client = TestClient(app)
        self.down = False
        self.calls = []


@pytest.fixture()
def edge(monkeypatch):
    from edge_server.endpoints import ap_states  # 3.9 で読み込めなければ、ここで失敗する

    central = FakeCentral(monkeypatch)

    class _Resp:
        def __init__(self, r):
            self.status_code, self.headers, self._r = r.status_code, r.headers, r

        def json(self):
            return self._r.json()

    def fake_request(method, url, **kw):
        central.calls.append((method, url))
        if central.down:
            raise httpx.ConnectError("central is down")
        path = url[url.index("/api/v1"):]
        return _Resp(central.client.request(method, path, json=kw.get("json")))

    monkeypatch.setattr(ap_states, "_http_request", fake_request)
    monkeypatch.setattr(ap_states, "local", ApStateAggregator(ap_ids=ap_ids_for(2)))
    app = FastAPI()
    app.include_router(ap_states.router)
    return TestClient(app), central, ap_states


def test_get_returns_only_configured_aps_with_fields(edge):
    client, _, _ = edge
    r = client.get("/api/v1/ap_states")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"AP0", "AP1"}  # AP2 は返さない
    for ap, state in body.items():
        assert set(state) >= {"tp_mbps", "rtt_ms", "n_connected", "measured", "ap_states_source"}, ap


def test_startup_counts_are_zero_not_four(edge):
    client, _, _ = edge
    body = client.get("/api/v1/ap_states").json()
    assert body["AP0"]["n_connected"] == 0 and body["AP1"]["n_connected"] == 0
    assert body["AP1"]["tp_mbps"] == pytest.approx(5.9, abs=0.01)  # 実測に合わせた既定
    assert body["AP0"]["rtt_ms"] == pytest.approx(47.0, abs=0.01)


def test_observation_via_one_edge_is_visible_through_central(edge):
    client, central, _ = edge
    assert client.post("/api/v1/ap_states/observe",
                       json={"terminal_id": "terminal-01", "ap": "AP1", "tp_mbps": 6.0, "rtt_ms": 40.0}).status_code == 200
    assert central.agg.snapshot()["AP1"]["n_connected"] == 1  # 中央に転送された
    r = client.get("/api/v1/ap_states")
    assert r.headers["X-Ap-States-Source"] == "central"
    b = r.json()["AP1"]
    assert b["n_connected"] == 1 and b["ap_states_source"] == "central"
    assert b["measured"]["tp_mbps"] == 6.0 and b["measured"]["n"] == 1


def test_other_edges_observation_counts_in_total(edge):
    """エッジ2 の端末の観測（中央に直接届いたもの）が、エッジ1 から見える。"""
    client, central, _ = edge
    central.agg.observe("terminal-03", "AP1", tp_mbps=5.0, rtt_ms=30.0)
    assert client.get("/api/v1/ap_states").json()["AP1"]["n_connected"] == 1


def test_central_down_falls_back_to_local_with_source_local(edge, caplog):
    client, central, _ = edge
    central.down = True
    r = client.post("/api/v1/ap_states/observe", json={"terminal_id": "terminal-01", "ap": "AP1", "tp_mbps": 6.0})
    assert r.status_code == 200  # 転送に失敗しても端末の計測は止めない
    caplog.clear()
    got = client.get("/api/v1/ap_states")
    assert got.status_code == 200
    assert got.headers["X-Ap-States-Source"] == "local"
    b = got.json()["AP1"]
    assert b["ap_states_source"] == "local" and b["n_connected"] == 1
    assert b["measured"]["tp_mbps"] == 6.0
    assert any("中央" in rec.getMessage() and rec.levelname == "WARNING" for rec in caplog.records)  # 警告がログに残る


def test_top_level_keys_are_only_aps_for_old_apk(edge):
    client, _, _ = edge
    body = client.get("/api/v1/ap_states").json()
    assert all(k.startswith("AP") and isinstance(v, dict) for k, v in body.items())


def test_old_apk_connect_still_works_and_repeats_do_not_inflate(edge):
    client, _, _ = edge
    for _ in range(24):
        r = client.post("/api/v1/ap_states/connect", json={"terminal_id": "terminal-01", "from_ap": "AP1", "to_ap": "AP0"})
        assert r.status_code == 200
    assert r.json()["connected"]["AP0"] == 1
    assert client.get("/api/v1/ap_states").json()["AP0"]["n_connected"] == 1


@pytest.mark.parametrize("payload", [
    {"terminal_id": "terminal-01", "to_ap": "AP1"},                      # from_ap を省略
    {"terminal_id": "terminal-01", "from_ap": None, "to_ap": "AP1"},     # from_ap が null
])
def test_first_connect_without_from_ap(edge, payload):
    client, _, _ = edge
    r = client.post("/api/v1/ap_states/connect", json=payload)
    assert r.status_code == 200
    assert r.json()["connected"]["AP1"] == 1 and r.json()["connected"]["AP0"] == 0


@pytest.mark.parametrize("path,payload", [
    ("/api/v1/ap_states/connect", {"terminal_id": "terminal-01", "from_ap": "AP0", "to_ap": "AP9"}),
    ("/api/v1/ap_states/connect", {"terminal_id": "terminal-01", "to_ap": "AP2"}),
    ("/api/v1/ap_states/observe", {"terminal_id": "terminal-01", "ap": "AP2", "tp_mbps": 1.0}),
])
def test_unknown_ap_is_rejected_and_counts_unchanged(edge, path, payload):
    client, central, _ = edge
    assert client.post(path, json=payload).status_code == 400
    assert all(s["n_connected"] == 0 for s in client.get("/api/v1/ap_states").json().values())
    assert all(s["n_connected"] == 0 for s in central.agg.snapshot().values())
