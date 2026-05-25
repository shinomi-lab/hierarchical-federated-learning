"""In-process test for `edge_infer` round-gate and broadcast behavior.

Runs FastAPI TestClient against the app, sets `current_edge_state.round` to
values below and at the gate threshold and calls `/edge/infer`.

Usage: python scripts/test_inference_round_gate.py
"""
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))
sys.path.append(str(Path(__file__).resolve().parent.parent / "edge_server"))

from fastapi.testclient import TestClient
import os
import asyncio

os.environ.setdefault('EDGE_INFERENCE_MIN_ROUND', '5')

from edge_server.main import app
from edge_server.state import current_edge_state

# capture broadcasts
captured = []
try:
    from edge_server.endpoints import notifications

    async def _capture(msg):
        captured.append(msg)

    # monkeypatch broadcast
    notifications.notifications_manager.broadcast = _capture
except Exception:
    pass

client = TestClient(app)

# Ensure app has a package pointing to an existing model under received_files
try:
    # common candidate in repo
    app.state.package = {"model_rel": "downloaded_model.pt"}
except Exception:
    pass

def call_infer(round_val):
    print(f"\n== Test round={round_val} ==")
    current_edge_state.round = round_val
    body = {
        "input": [
            {"tpNeed": 0.5, "rttNeed": 0.1, "appNum": 1},
            {"tpNeed": 0.2, "rttNeed": 0.3, "appNum": 2}
        ]
    }
    resp = client.post("/edge/infer", json=body)
    print("status:", resp.status_code)
    try:
        print("json:", resp.json())
    except Exception:
        print("text:", resp.text)

if __name__ == '__main__':
    call_infer(4)  # expect locked -> 409
    call_infer(5)  # expect success
    print('\nCaptured broadcasts count:', len(captured))
    if captured:
        print('sample payload:', captured[0])
