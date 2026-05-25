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
from edge_server.config import RECEIVED_DIR
from edge_server.state import current_edge_state


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
    meta_path = d / "weight.meta.json"
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
    base = Path(RECEIVED_DIR)
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
    rel_meta = f"{dirname}/weight.meta.json"
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
