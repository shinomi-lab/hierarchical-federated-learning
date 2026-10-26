# Serverside_HFL/tests/test_hfl_rec_infer.py
import json
import random
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from central_server.endpoints import central_infer as ci
from central_server.endpoints import hfl_rec_infer as hr
from shared.central_model import state_sha256
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


def _set_round(tmp_path, rnd):
    (tmp_path / "current_meta.json").write_text(json.dumps({"round": rnd}), encoding="utf-8")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    csv_path = tmp_path / "t.csv"
    _write_csv(csv_path)
    state = tmp_path / "state"
    tcm.train(csv_path, state, epochs=2, seed=3)  # state/central_model.pt と norm_stats.json
    shutil.copy(state / "central_model.pt", tmp_path / "global_model_mobile.pt")
    (tmp_path / "app.json").write_text(json.dumps(APPS, ensure_ascii=False), encoding="utf-8")
    _set_round(tmp_path, 4)
    monkeypatch.setattr(hr, "GLOBAL_MODEL_PATH", tmp_path / "global_model_mobile.pt")
    monkeypatch.setattr(hr, "META_PATH", tmp_path / "current_meta.json")
    monkeypatch.setattr(hr, "APPS_PATH", tmp_path / "app.json")
    monkeypatch.setattr(hr, "NORM_STATS_PATH", state / "norm_stats.json")
    monkeypatch.setattr(hr, "LOG_PATH", tmp_path / "hfl_rec_infer.jsonl")
    monkeypatch.setattr(ci, "LOG_PATH", tmp_path / "central_infer.jsonl")
    hr.reset_cache()
    return tmp_path


@pytest.fixture
def client(paths):
    app = FastAPI()
    app.include_router(hr.router)
    return TestClient(app)


def log_lines(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def post(client, app_type="動画", **extra):
    return client.post("/api/v1/hfl_rec_infer", json={"terminal_id": "terminal-01", "app_type": app_type, **extra})


def test_aggregated_model_response(client, paths):
    r = post(client)
    assert r.status_code == 200
    j = r.json()
    import torch
    assert j["model_sha256"] == state_sha256(torch.load(str(paths / "global_model_mobile.pt"), map_location="cpu"))
    assert j["round"] == 4 and j["is_initial"] is False
    assert len(j["scores"]) == 2 and abs(sum(j["scores"]) - 1.0) < 1e-5
    assert j["chosen_ap"] == j["scores"].index(max(j["scores"]))
    assert j["server_ms"] >= 0 and j["model_mtime"]


def test_round_1_is_marked_initial(client, paths):
    _set_round(paths, 1)
    j = post(client).json()
    assert j["round"] == 1 and j["is_initial"] is True


def test_independent_of_measured_values(client):
    a = post(client, "通話", tp_measured_mbps=1.0, rtt_measured_ms=500).json()
    b = post(client, "通話", tp_measured_mbps=90.0, rtt_measured_ms=3).json()
    assert a["chosen_ap"] == b["chosen_ap"] and a["scores"] == b["scores"]


def test_no_global_model_is_503_not_initial_fallback(client, paths):
    (paths / "global_model_mobile.pt").unlink()
    r = post(client)
    assert r.status_code == 503
    rows = log_lines(paths / "hfl_rec_infer.jsonl")
    assert len(rows) == 1 and rows[0]["status"] == 503 and rows[0]["chosen_ap"] is None


def test_unknown_app_is_400(client, paths):
    r = post(client, "存在しないアプリ")
    assert r.status_code == 400
    assert log_lines(paths / "hfl_rec_infer.jsonl")[0]["chosen_ap"] is None


def test_one_log_row_per_query_and_central_log_untouched(client, paths):
    post(client)
    post(client, "通話")
    rows = log_lines(paths / "hfl_rec_infer.jsonl")
    assert len(rows) == 2
    for k in ("ts", "terminal_id", "app_type", "chosen_ap", "model_sha256", "round", "is_initial", "status", "server_ms"):
        assert k in rows[0]
    assert not (paths / "central_infer.jsonl").exists()


def test_cache_refreshes_when_model_is_replaced(client, paths):
    sha1 = post(client).json()["model_sha256"]
    tcm.train(paths / "t.csv", paths / "state2", epochs=2, seed=99)
    shutil.copy(paths / "state2" / "central_model.pt", paths / "global_model_mobile.pt")
    assert post(client).json()["model_sha256"] != sha1


def test_server_starts_without_model(paths):
    (paths / "global_model_mobile.pt").unlink()
    assert hr.load_model() is False  # 例外にならない
