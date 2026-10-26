# Serverside_HFL/tests/test_central_infer.py
import json
import random

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from central_server.endpoints import central_infer as ci
from tools import train_central_model as tcm

APPS = [
    {"appType": "ブラウザ", "needTP": 5, "needRTT": 0},
    {"appType": "動画", "needTP": 10, "needRTT": 0},
    {"appType": "通話", "needTP": 0, "needRTT": 200},
    {"appType": "配信", "needTP": 0, "needRTT": 100},
]


def _write_csv(path, n=200):
    rnd = random.Random(0)
    with open(path, "w", encoding="utf-8") as f:
        for _ in range(n):
            a = rnd.randrange(4)
            f.write(f"{APPS[a]['needTP']},{APPS[a]['needRTT']},{a},{0 if a < 2 else 1}\n")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    csv_path = tmp_path / "t.csv"
    _write_csv(csv_path)
    state = tmp_path / "state"
    tcm.train(csv_path, state, epochs=2, seed=3)
    (tmp_path / "app.json").write_text(json.dumps(APPS, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ci, "MODEL_PATH", state / "central_model.pt")
    monkeypatch.setattr(ci, "META_PATH", state / "central_model.meta.json")
    monkeypatch.setattr(ci, "APPS_PATH", tmp_path / "app.json")
    monkeypatch.setattr(ci, "NORM_STATS_PATH", state / "norm_stats.json")
    monkeypatch.setattr(ci, "LOG_PATH", tmp_path / "central_infer.jsonl")
    ci.reset_cache()
    return tmp_path


@pytest.fixture
def client(paths):
    app = FastAPI()
    app.include_router(ci.router)
    return TestClient(app)


def log_lines(tmp_path):
    p = tmp_path / "central_infer.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_normal_response(client, paths):
    r = client.post("/api/v1/central_infer", json={"terminal_id": "terminal-01", "app_type": "動画"})
    assert r.status_code == 200
    j = r.json()
    meta = json.loads((paths / "state" / "central_model.meta.json").read_text(encoding="utf-8"))
    assert j["model_sha256"] == meta["model_sha256"]
    assert len(j["scores"]) == 2 and abs(sum(j["scores"]) - 1.0) < 1e-5
    assert j["chosen_ap"] == j["scores"].index(max(j["scores"]))
    assert j["server_ms"] >= 0


def test_independent_of_measured_values(client):
    a = client.post("/api/v1/central_infer", json={"terminal_id": "t1", "app_type": "通話", "tp_measured_mbps": 1.0, "rtt_measured_ms": 500}).json()
    b = client.post("/api/v1/central_infer", json={"terminal_id": "t2", "app_type": "通話", "tp_measured_mbps": 90.0, "rtt_measured_ms": 3}).json()
    assert a["chosen_ap"] == b["chosen_ap"] and a["scores"] == b["scores"]


def test_unknown_app_is_4xx_and_not_inferred(client, paths):
    r = client.post("/api/v1/central_infer", json={"terminal_id": "t1", "app_type": "存在しないアプリ"})
    assert 400 <= r.status_code < 500
    rows = log_lines(paths)
    assert len(rows) == 1 and rows[0]["status"] == r.status_code and rows[0].get("chosen_ap") is None


def test_missing_fields_is_4xx(client):
    assert 400 <= client.post("/api/v1/central_infer", json={"terminal_id": "t1"}).status_code < 500


def test_model_not_ready_is_5xx_with_reason(client, paths):
    (paths / "state" / "central_model.pt").unlink()
    ci.reset_cache()
    r = client.post("/api/v1/central_infer", json={"terminal_id": "t1", "app_type": "動画"})
    assert 500 <= r.status_code < 600
    assert "central_model" in json.dumps(r.json(), ensure_ascii=False)


def test_one_log_line_per_query(client, paths):
    for i in range(3):
        client.post("/api/v1/central_infer", json={"terminal_id": f"terminal-0{i}", "app_type": "配信"})
    rows = log_lines(paths)
    assert len(rows) == 3
    r = rows[1]
    assert r["terminal_id"] == "terminal-01" and r["app_type"] == "配信" and r["status"] == 200
    assert r["chosen_ap"] in (0, 1) and r["model_sha256"] and r["server_ms"] >= 0 and r["ts"]


def test_uses_normalization_from_model_meta(client, paths):
    """正規化は中央モデルの学習時の統計（メタ）を使う。norm_stats.json が別の値でも結果は変わらない。"""
    a = client.post("/api/v1/central_infer", json={"terminal_id": "t1", "app_type": "通話"}).json()
    (paths / "state" / "norm_stats.json").write_text(
        json.dumps({"tp_mean": 99.0, "tp_std": 9.0, "rtt_mean": 99.0, "rtt_std": 9.0}), encoding="utf-8")
    ci.reset_cache()
    b = client.post("/api/v1/central_infer", json={"terminal_id": "t1", "app_type": "通話"}).json()
    assert a["scores"] == b["scores"]


def test_load_model_does_not_raise_when_missing(paths):
    assert ci.load_model() is True
    (paths / "state" / "central_model.pt").unlink()
    ci.reset_cache()
    assert ci.load_model() is False  # 起動を止めない（例外にしない）


def test_route_is_registered_in_central_app():
    # central_server.main を import すると追跡対象の current_meta.json とログが書き換わるため、ソースで確認する
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "central_server" / "main.py").read_text(encoding="utf-8")
    assert "from central_server.endpoints import central_infer" in src
    assert "app.include_router(central_infer.router)" in src
