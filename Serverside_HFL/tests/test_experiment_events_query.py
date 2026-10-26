# Serverside_HFL/tests/test_experiment_events_query.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from central_server.endpoints import experiment_control as ec


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ec, "CONTROL_PATH", tmp_path / "experiment_control.json")
    monkeypatch.setattr(ec, "EVENTS_PATH", tmp_path / "experiment_events.jsonl")
    app = FastAPI()
    app.include_router(ec.router)
    return TestClient(app)


def _done(tid, tag, result="ok"):
    return {"type": "session_complete", "terminal_id": tid, "run_id": f"r-{tid}", "run_tag": tag,
            "result": result, "hfl_mode": "sync", "hfl_mode_source": "server", "num_cycles": 5,
            "after_switch_confirmed": "true"}


def test_list_session_complete_by_run_tag(client):
    for tid in ("terminal-01", "terminal-02"):
        assert client.post("/api/v1/experiment_events", json=_done(tid, "T001")).status_code == 200
    client.post("/api/v1/experiment_events", json=_done("terminal-03", "T000"))
    client.post("/api/v1/experiment_events", json={"type": "control_changed"})
    r = client.get("/api/v1/experiment_events", params={"type": "session_complete", "run_tag": "T001"})
    assert r.status_code == 200
    assert sorted(e["terminal_id"] for e in r.json()["events"]) == ["terminal-01", "terminal-02"]


def test_since_filters_older_events():
    evs = [{"type": "session_complete", "received_at": "2026-10-14T01:00:00"},
           {"type": "session_complete", "received_at": "2026-10-14T03:00:00"}]
    assert len(ec.filter_events(evs, "session_complete", "2026-10-14T02:00:00", None)) == 1


def test_bad_since_is_400(client):
    assert client.get("/api/v1/experiment_events", params={"since": "yesterday"}).status_code == 400
