# Serverside_HFL/tests/test_experiment_control.py
import json

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


def events(tmp_path):
    p = tmp_path / "experiment_events.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_default_is_unset(client):
    assert client.get("/api/v1/experiment_control").json()["baseline_mode"] is None


def test_set_global_and_override_then_resolve(client, tmp_path):
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "fixed"}).status_code == 200
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "random", "terminal_id": "terminal-02"}).status_code == 200
    c = client.get("/api/v1/experiment_control").json()
    assert ec.resolve_mode(c, "terminal-02") == "random"
    assert ec.resolve_mode(c, "terminal-01") == "fixed"
    assert c["version"] == 2
    assert [e["type"] for e in events(tmp_path)] == ["control_changed", "control_changed"]


def test_clear_override_and_global(client):
    client.put("/api/v1/experiment_control", json={"baseline_mode": "fixed"})
    client.put("/api/v1/experiment_control", json={"baseline_mode": "random", "terminal_id": "t2"})
    client.put("/api/v1/experiment_control", json={"clear": True, "terminal_id": "t2"})
    client.put("/api/v1/experiment_control", json={"clear": True})
    c = client.get("/api/v1/experiment_control").json()
    assert c["baseline_mode"] is None and c["overrides"] == {}
    assert ec.resolve_mode(c, "t2") is None


def test_unknown_mode_is_400(client):
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "bogus"}).status_code == 400


def test_persisted_across_reload(client, tmp_path):
    client.put("/api/v1/experiment_control", json={"baseline_mode": "greedy"})
    assert json.loads((tmp_path / "experiment_control.json").read_text())["baseline_mode"] == "greedy"


def test_event_append(client, tmp_path):
    body = {"type": "baseline_mode_change", "terminal_id": "t1", "from": "none", "to": "fixed", "undelivered": 0}
    assert client.post("/api/v1/experiment_events", json=body).status_code == 200
    assert events(tmp_path)[-1]["to"] == "fixed" and "received_at" in events(tmp_path)[-1]


# --- SP3d: サイクル数（全体既定・端末上書き） ---
def test_num_cycles_global_and_override(client, tmp_path):
    assert client.put("/api/v1/experiment_control", json={"num_cycles": 20}).status_code == 200
    assert client.put("/api/v1/experiment_control", json={"num_cycles": 3, "terminal_id": "terminal-02"}).status_code == 200
    c = client.get("/api/v1/experiment_control").json()
    assert ec.resolve_num_cycles(c, "terminal-02") == 3
    assert ec.resolve_num_cycles(c, "terminal-01") == 20
    assert [e["type"] for e in events(tmp_path)] == ["control_changed", "control_changed"]
    assert events(tmp_path)[-1]["after"]["num_cycles_overrides"] == {"terminal-02": 3}
    assert events(tmp_path)[-1]["before"]["num_cycles"] == 20


def test_num_cycles_does_not_touch_baseline(client):
    client.put("/api/v1/experiment_control", json={"baseline_mode": "random", "terminal_id": "t2"})
    client.put("/api/v1/experiment_control", json={"num_cycles": 10, "terminal_id": "t2"})
    c = client.get("/api/v1/experiment_control").json()
    assert ec.resolve_mode(c, "t2") == "random" and ec.resolve_num_cycles(c, "t2") == 10


def test_baseline_does_not_touch_num_cycles(client):
    client.put("/api/v1/experiment_control", json={"num_cycles": 7, "terminal_id": "t2"})
    client.put("/api/v1/experiment_control", json={"clear": True, "terminal_id": "t2"})
    assert ec.resolve_num_cycles(client.get("/api/v1/experiment_control").json(), "t2") == 7


def test_baseline_and_num_cycles_together(client):
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "fixed", "num_cycles": 20}).status_code == 200
    c = client.get("/api/v1/experiment_control").json()
    assert c["baseline_mode"] == "fixed" and c["num_cycles"] == 20 and c["version"] == 1


def test_clear_num_cycles(client):
    client.put("/api/v1/experiment_control", json={"num_cycles": 20})
    client.put("/api/v1/experiment_control", json={"num_cycles": 3, "terminal_id": "t2"})
    client.put("/api/v1/experiment_control", json={"clear_num_cycles": True, "terminal_id": "t2"})
    assert ec.resolve_num_cycles(client.get("/api/v1/experiment_control").json(), "t2") == 20
    client.put("/api/v1/experiment_control", json={"clear_num_cycles": True})
    assert ec.resolve_num_cycles(client.get("/api/v1/experiment_control").json(), "t2") is None


@pytest.mark.parametrize("bad", [0, 101, -1, 2.5, "20", True, None])
def test_num_cycles_out_of_range_is_400(client, tmp_path, bad):
    assert client.put("/api/v1/experiment_control", json={"num_cycles": bad}).status_code == 400
    c = client.get("/api/v1/experiment_control").json()
    assert c["num_cycles"] is None and c["version"] == 0
    assert events(tmp_path) == []


def test_invalid_num_cycles_does_not_apply_baseline_either(client):
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "fixed", "num_cycles": 0}).status_code == 400
    assert client.get("/api/v1/experiment_control").json()["baseline_mode"] is None


def test_old_control_file_without_num_cycles_loads(client, tmp_path):
    (tmp_path / "experiment_control.json").write_text('{"baseline_mode": "fixed", "overrides": {}, "version": 4}')
    c = client.get("/api/v1/experiment_control").json()
    assert c["num_cycles"] is None and c["num_cycles_overrides"] == {} and c["baseline_mode"] == "fixed"


def test_body_without_update_fields_is_400(client):
    assert client.put("/api/v1/experiment_control", json={"terminal_id": "t2"}).status_code == 400


# --- centralized-mode: 実験モード central ---
def test_central_mode_global_and_override_with_events(client, tmp_path):
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "central", "terminal_id": "terminal-02"}).status_code == 200
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "fixed"}).status_code == 200
    c = client.get("/api/v1/experiment_control").json()
    assert ec.resolve_mode(c, "terminal-02") == "central"
    assert ec.resolve_mode(c, "terminal-01") == "fixed"
    ev = events(tmp_path)
    assert [e["type"] for e in ev] == ["control_changed", "control_changed"]
    assert ev[0]["after"]["overrides"] == {"terminal-02": "central"}


def test_central_mode_as_global_default_and_cleared(client):
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "central"}).status_code == 200
    assert ec.resolve_mode(client.get("/api/v1/experiment_control").json(), "terminal-01") == "central"
    client.put("/api/v1/experiment_control", json={"clear": True})
    assert client.get("/api/v1/experiment_control").json()["baseline_mode"] is None


def test_near_miss_of_central_is_still_rejected(client):
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "central2"}).status_code == 400
    assert client.get("/api/v1/experiment_control").json()["version"] == 0


# --- hfl-recommended-ap-mode: 実験モード hfl_rec ---
def test_hfl_rec_mode_global_and_override_with_events(client, tmp_path):
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "hfl_rec", "terminal_id": "terminal-02"}).status_code == 200
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "fixed"}).status_code == 200
    c = client.get("/api/v1/experiment_control").json()
    assert ec.resolve_mode(c, "terminal-02") == "hfl_rec"
    assert ec.resolve_mode(c, "terminal-01") == "fixed"
    ev = events(tmp_path)
    assert [e["type"] for e in ev] == ["control_changed", "control_changed"]
    assert ev[0]["after"]["overrides"] == {"terminal-02": "hfl_rec"}


def test_hfl_rec_mode_as_global_default_and_cleared(client):
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "hfl_rec"}).status_code == 200
    assert ec.resolve_mode(client.get("/api/v1/experiment_control").json(), "terminal-01") == "hfl_rec"
    client.put("/api/v1/experiment_control", json={"clear": True})
    assert client.get("/api/v1/experiment_control").json()["baseline_mode"] is None


def test_near_miss_of_hfl_rec_is_rejected_and_existing_modes_unchanged(client):
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "hfl_rec2"}).status_code == 400
    assert client.get("/api/v1/experiment_control").json()["version"] == 0
    assert ec.MODES[:5] == ("none", "fixed", "random", "greedy", "central")


# --- fix-audit-20261026: fixed_ap1 ---
def test_fixed_ap1_global_and_override_with_events(client, tmp_path):
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "fixed_ap1", "terminal_id": "terminal-02"}).status_code == 200
    c = client.get("/api/v1/experiment_control").json()
    assert ec.resolve_mode(c, "terminal-02") == "fixed_ap1"
    assert events(tmp_path)[0]["after"]["overrides"] == {"terminal-02": "fixed_ap1"}


def test_fixed_ap2_is_rejected(client):
    assert client.put("/api/v1/experiment_control", json={"baseline_mode": "fixed_ap2"}).status_code == 400
    assert client.get("/api/v1/experiment_control").json()["version"] == 0
