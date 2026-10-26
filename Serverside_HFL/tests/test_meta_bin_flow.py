import os
import json
import hashlib
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

# Ensure project root is importable when running pytest from workspace
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from edge_server.main import create_app
from edge_server import startup
from edge_server.endpoints import terminal_update
from edge_server.state import current_edge_state

# 2026-10 (SP3b 2.5) の修正点:
# - メタのファイル名は変換ツール tools/pt_to_meta_weights.py が実際に書く "meta.json"（端末も bin と同じ場所の
#   meta.json を読む）。旧テストは "weight.meta.json" を作っており、/send_to_device の候補
#   （<model>.pt.meta.json / meta.json / <stem>.meta.json）に無いため model_meta が出ず失敗していた。
# - 旧テストは実際の received_files/ に "99999999_000000" を作っていた。F10 以降の /send_to_device は
#   ディスク上の最も新しい変換済みバッチを配るため、テスト後にエッジを起動するとこのテスト用の重みが端末に配られる。
#   配布ディレクトリと端末更新の保存先を tmp_path に向けて、実データに触れないようにする。


def make_test_package(base_dir: Path, dirname: str = "99999999_000000"):
    d = base_dir / dirname
    d.mkdir(parents=True, exist_ok=True)
    # create a small weight.bin with a single tensor (2x3 float32)
    arr = np.arange(6, dtype=np.float32).reshape((2, 3)) + 0.5
    bin_path = d / "weight.bin"
    arr.tofile(str(bin_path))
    # compute sha256
    with open(bin_path, "rb") as f:
        b = f.read()
    sha = hashlib.sha256(b).hexdigest()
    meta = {
        "format_version": 1,
        "model_version": "test_r1",
        "framework": "pytorch_state_dict",
        "dtype": "float32",
        "endianness": "little",
        "weights_size": len(b),
        "weights_sha256": sha,
        "created_at": "2025-12-17T00:00:00Z",
        "media_type": "application/octet-stream",
        "tensors": [
            {"name": "w", "shape": [2, 3], "offset": 0, "length_bytes": len(b)}
        ],
    }
    meta_path = d / "meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    # Also write a model .pt fallback file for legacy
    pt_path = d / "global_model_mobile.pt"
    with open(pt_path, "wb") as f:
        f.write(b"PTPLACEHOLDER")
    return dirname, str(bin_path), str(meta_path)


def test_meta_bin_flow(tmp_path, monkeypatch):
    # Ensure edge startup does not attempt central sync
    monkeypatch.setenv("SKIP_CENTRAL_SYNC", "1")
    base = (tmp_path / "received_files").resolve()
    base.mkdir()
    monkeypatch.setattr(startup, "BASE_DIR", base)
    updates = base / "terminal_updates"
    monkeypatch.setattr(terminal_update, "TERMINAL_UPDATE_DIR", updates)
    monkeypatch.setattr(terminal_update, "RECEIVED_HASHES_FILE", updates / "received_hashes.json")
    # create package
    dirname, bin_p, meta_p = make_test_package(base)

    app = create_app()
    client = TestClient(app)
    # Ensure send_to_device sees our package (bypass disk latest selection)
    client.app.state.package = {"model_rel": f"{dirname}/global_model_mobile.pt"}

    # 1) /send_to_device should advertise model_meta and model_bin
    r = client.get("/send_to_device")
    assert r.status_code == 200
    j = r.json()
    assert "links" in j
    links = j["links"]
    assert links.get("model_meta") is not None
    assert links.get("model_bin") is not None

    # 2) download meta and bin via /download
    rel_meta = f"{dirname}/meta.json"
    rm = client.get("/download", params={"rel_path": rel_meta})
    assert rm.status_code == 200
    meta_data = rm.content
    loaded_meta = json.loads(meta_data.decode("utf-8"))
    assert loaded_meta["weights_sha256"] == loaded_meta["weights_sha256"]

    rel_bin = f"{dirname}/weight.bin"
    rb = client.get("/download", params={"rel_path": rel_bin})
    assert rb.status_code == 200
    # verify sha
    sha = hashlib.sha256(rb.content).hexdigest()
    assert sha == loaded_meta["weights_sha256"]

    # 3) upload meta+bin to /receive_terminal_weights/{terminal_id}
    from edge_server.state import current_edge_state
    round_id = int(current_edge_state.round)

    files = {
        "weights": ("weight.bin", open(bin_p, "rb"), "application/octet-stream"),
        "meta_file": ("meta.json", open(meta_p, "rb"), "application/json"),
    }
    data = {
        "round_id": str(round_id),
        "model_id": "test_model",
        "base_hash": "bh",
        "n_samples": "1",
        "dtype": "meta_bin",
    }
    ru = client.post(f"/receive_terminal_weights/device-001", data=data, files=files)
    assert ru.status_code == 200, ru.text
    jr = ru.json()
    assert jr.get("status") == "weights_received"
