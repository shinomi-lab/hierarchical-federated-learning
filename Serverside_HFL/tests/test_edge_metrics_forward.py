# Serverside_HFL/tests/test_edge_metrics_forward.py
import asyncio
import json

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from edge_server.endpoints import terminal_update as tu


def _run(coro):
    # asyncio.run は終了時に現在のイベントループを外し、後続テスト（get_event_loop 依存）を壊すため専用ループで実行
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client(monkeypatch, status):
    sent = {}

    async def fake_forward(body, transport=None):
        sent["body"] = body
        return status, "x"

    monkeypatch.setattr(tu, "forward_metric_to_central", fake_forward)
    app = FastAPI()
    app.include_router(tu.router)
    return TestClient(app), sent


def test_central_ok_returns_200_and_forwards_all_fields(monkeypatch):
    c, sent = _client(monkeypatch, 200)
    body = {"data_id": "d", "terminal_id": "t", "epoch_val_accuracy": [0.1], "run_id": "r"}
    assert c.post("/upload_training_metrics", json=body).status_code == 200
    assert sent["body"]["epoch_val_accuracy"] == [0.1] and sent["body"]["run_id"] == "r"


def test_central_down_returns_502(monkeypatch):
    c, _ = _client(monkeypatch, 0)
    assert c.post("/upload_training_metrics", json={"data_id": "d", "terminal_id": "t"}).status_code == 502


def test_central_500_returns_502_and_400_passes_through(monkeypatch):
    c, _ = _client(monkeypatch, 500)
    assert c.post("/upload_training_metrics", json={"data_id": "d", "terminal_id": "t"}).status_code == 502
    c, _ = _client(monkeypatch, 400)
    assert c.post("/upload_training_metrics", json={"data_id": "d", "terminal_id": "t"}).status_code == 400


def test_forward_adds_edge_id_and_posts_json():
    seen = {}

    def handler(req: httpx.Request):
        seen["json"] = json.loads(req.content)
        seen["ctype"] = req.headers["content-type"]
        return httpx.Response(200, json={"status": "success"})

    code, _ = _run(tu.forward_metric_to_central({"data_id": "d", "terminal_id": "t"},
                                                       transport=httpx.MockTransport(handler)))
    assert code == 200
    assert seen["ctype"].startswith("application/json")
    assert seen["json"]["edge_id"] == tu.EDGE_SERVER_ID and seen["json"]["data_id"] == "d"


def test_forward_connection_error_is_zero():
    def handler(req):
        raise httpx.ConnectError("down")

    code, _ = _run(tu.forward_metric_to_central({"data_id": "d", "terminal_id": "t"},
                                                       transport=httpx.MockTransport(handler)))
    assert code == 0


def test_metric_from_client_meta():
    cm = json.dumps({"accuracy": 0.1, "metric": {"data_id": "d-9", "accuracy": 0.7}})
    m = tu.metric_from_client_meta(cm, "terminal-05")
    assert m == {"data_id": "d-9", "accuracy": 0.7, "terminal_id": "terminal-05"}
    assert tu.metric_from_client_meta(json.dumps({"accuracy": 0.1}), "t") is None
    assert tu.metric_from_client_meta("not json", "t") is None
    assert tu.metric_from_client_meta(None, "t") is None
