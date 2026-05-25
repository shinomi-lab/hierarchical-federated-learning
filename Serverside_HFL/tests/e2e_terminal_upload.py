import io
import hashlib
import json
import numpy as np
from fastapi.testclient import TestClient
from edge_server.main import create_app
from edge_server.state import current_edge_state


def build_full_mlp_flat(I, H, O):
    # spec_for_full_mlp ordering must match the server; construct simple float sequence
    spec = [
        ("layer1.weight", (H, I)),
        ("layer1.bias",   (H,)),
        ("norm1.weight",  (H,)),
        ("norm1.bias",    (H,)),
        ("layer2.weight", (H, H)),
        ("layer2.bias",   (H,)),
        ("norm2.weight",  (H,)),
        ("norm2.bias",    (H,)),
        ("layer3.weight",    (O, H)),
        ("layer3.bias",      (O,)),
    ]
    vals = []
    cnt = 0
    for _k, shape in spec:
        n = 1
        for s in shape:
            n *= int(s)
        for i in range(n):
            vals.append(float((cnt % 100) + 0.5))
            cnt += 1
    arr = np.array(vals, dtype=np.float32)
    return arr


def test_e2e_receive_f32_flat():
    app = create_app()
    # ensure server round matches
    current_edge_state.round = 1

    client = TestClient(app)

    # build small model dims
    I, H, O = 2, 2, 1
    arr = build_full_mlp_flat(I, H, O)
    bio = io.BytesIO()
    bio.write(arr.tobytes())
    bio.seek(0)

    sha = "sha256:" + hashlib.sha256(bio.getvalue()).hexdigest()

    files = {
        "weights": ("weights.bin", bio, "application/octet-stream"),
    }
    data = {
        "round_id": str(1),
        "model_id": "test-mlp",
        "base_hash": "sha256:bootstrap",
        "n_samples": str(10),
        "payload_kind": "full",
        "dtype": "f32_flat",
        "input_size": str(I),
        "hidden_size": str(H),
        "output_size": str(O),
        "contentSha256": sha,
        "run_id": "test-run-1",
    }

    resp = client.post("/receive_terminal_weights/device-001", data=data, files=files)
    assert resp.status_code == 200, resp.text
    jb = resp.json()
    assert jb.get("ack") is True or jb.get("status") in ("weights_received", "duplicate_ignored")
