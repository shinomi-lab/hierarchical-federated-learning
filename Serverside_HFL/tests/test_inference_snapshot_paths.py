"""推論スナップショットが読むモデルとラウンド（fix-audit-20261015 E6）。"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import start  # noqa: E402


def test_uses_storage_model_and_meta_round(tmp_path):
    storage = tmp_path / "storage" / "state"
    storage.mkdir(parents=True)
    (storage / "global_model_mobile.pt").write_bytes(b"x")
    (storage / "current_meta.json").write_text(json.dumps({"round": 3}))
    initial = tmp_path / "code" / "state" / "global_model_mobile.pt"
    initial.parent.mkdir(parents=True)
    initial.write_bytes(b"init")
    src = start._snapshot_sources(storage / "global_model_mobile.pt", initial, storage / "current_meta.json")
    assert src["model_path"] == storage / "global_model_mobile.pt"
    assert src["is_initial"] is False and src["round"] == 3 and src["mtime"]


def test_falls_back_to_initial_model(tmp_path):
    initial = tmp_path / "init.pt"
    initial.write_bytes(b"init")
    src = start._snapshot_sources(tmp_path / "none.pt", initial, tmp_path / "none.json")
    assert src["model_path"] == initial and src["is_initial"] is True and src["round"] is None


def test_same_paths_as_central_under_storage_dir(tmp_path, monkeypatch):
    """HFL_STORAGE_DIR を指定すると、スナップショットの既定の読み先は中央の保存先になる。"""
    monkeypatch.setenv("HFL_STORAGE_DIR", str(tmp_path))
    model, meta, db = start._central_storage_paths()
    assert model == (tmp_path / "state" / "global_model_mobile.pt").resolve()
    assert meta == (tmp_path / "state" / "current_meta.json").resolve()
    assert db == (tmp_path / "received_edges" / "training_metrics.db").resolve()


def test_round_one_is_initial_even_if_same_path(tmp_path):
    """リポジトリから直接起動（保存先 = コードの state/）でも、ラウンド 1（集約前）なら初期モデルと示す。"""
    model = tmp_path / "global_model_mobile.pt"
    model.write_bytes(b"init")
    meta = tmp_path / "current_meta.json"
    meta.write_text(json.dumps({"round": 1}))
    src = start._snapshot_sources(model, model, meta)
    assert src["is_initial"] is True and src["round"] == 1
    meta.write_text(json.dumps({"round": 2}))
    assert start._snapshot_sources(model, model, meta)["is_initial"] is False
