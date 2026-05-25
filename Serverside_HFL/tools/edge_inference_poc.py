import asyncio
import json
import time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn
import requests
from typing import Dict, Any

app = FastAPI()

clients: Dict[str, WebSocket] = {}

# Handover penalty (ms) added when switching connected edge
HANDOVER_PENALTY_MS = int(50)
# track previous connected server per device
previous_connected: Dict[str, str] = {}

CENTRAL_URL = "http://127.0.0.1:9002"


def rtt_probe_sync(url: str, timeout: float = 1.0) -> float:
    start = time.time()
    try:
        r = requests.get(url, timeout=timeout)
        elapsed = time.time() - start
        return elapsed
    except Exception:
        return float("inf")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    device_id = None
    try:
        msg = await ws.receive_text()
        data = json.loads(msg)
        device_id = data.get("device_id")
        edge_id = data.get("edge_id", "edge-local")
        clients[device_id] = ws
        # initialize previous connected server to the endpoint the client reports
        # this allows the simulation to penalize switches away from current server
        try:
            prev_ep = data.get("endpoint") or data.get("edge_id") or None
            if prev_ep:
                previous_connected[device_id] = prev_ep
            else:
                previous_connected[device_id] = None
        except Exception:
            previous_connected[device_id] = None
        # register to central index if available
        try:
            requests.post(f"{CENTRAL_URL}/register_connection", json={
                "device_id": device_id,
                "edge_id": edge_id,
                "endpoint": data.get("endpoint", "ws://unknown"),
                "model_version": data.get("model_version"),
            }, timeout=1.0)
        except Exception:
            pass

        # basic loop: accept pings and send proposals
        # maintain EMA-smoothed RTTs across iterations
        ema = {}
        alpha = 0.3
        while True:
            await asyncio.sleep(5)
            candidates = ["http://127.0.0.1:9001", "http://127.0.0.1:9003"]
            loop = asyncio.get_event_loop()
            rtts = []
            for c in candidates:
                r = await loop.run_in_executor(None, rtt_probe_sync, c)
                    # apply handover penalty if switching from previous connected server
                    penalty_sec = 0.0
                    try:
                        prev = previous_connected.get(device_id)
                        # treat candidate string (URL) as server id; if different from prev and prev exists, penalize
                        if prev is not None and prev != c:
                            penalty_sec = float(HANDOVER_PENALTY_MS) / 1000.0
                    except Exception:
                        penalty_sec = 0.0

                    # treat inf as None for JSON safety; keep numeric for EMA
                    val = None if r == float('inf') else (r + penalty_sec)
                prev = ema.get(c)
                if prev is None:
                        ema[c] = (r + penalty_sec) if r != float('inf') else None
                else:
                        if r == float('inf'):
                            # keep previous EMA
                            ema[c] = prev
                        else:
                            ema[c] = alpha * (r + penalty_sec) + (1 - alpha) * prev
                rtts.append((c, val))
            # sort by numeric EMA where available; missing treated as large
            def score(item):
                c, _ = item
                v = ema.get(c)
                return v if v is not None else 1e9
            rtts_sorted = sorted(rtts, key=score)
            preferred = rtts_sorted[0][0]
            proposal = {
                "type": "switch_proposal",
                "preferred": preferred,
                "rtts": [{"url": u, "rtt": (None if r is None else r)} for u, r in rtts_sorted],
                "timestamp": int(time.time()),
            }
            try:
                await ws.send_text(json.dumps(proposal))
            except Exception:
                break

            # update previous_connected to the chosen preferred endpoint
            try:
                previous_connected[device_id] = preferred
            except Exception:
                pass

    except WebSocketDisconnect:
        pass
    finally:
        try:
            if device_id and device_id in clients:
                del clients[device_id]
        except Exception:
            pass


@app.post("/edge/load_model")
def load_model(model_url: str):
    # placeholder for model load endpoint
    return {"ok": True, "loaded": model_url}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9001)
