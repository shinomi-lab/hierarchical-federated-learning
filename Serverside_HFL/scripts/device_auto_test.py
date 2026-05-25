#!/usr/bin/env python3
"""
Simple device-side automated test client.
- Connects to Edge WebSocket `/ws/updates`
- Waits for a `model_update` message
- Sends `MODEL_LOAD_START`, downloads the model (via `/download?rel_path=...`), sends `MODEL_LOAD_END` (includes elapsed_ms)
- Sends `DATASET_INIT_START` / `DATASET_READY` (simulated wait)
- Sends `TRAINING_CYCLE_START`
- Sends final `device_ack`

Usage:
  python scripts\device_auto_test.py --edge http://localhost:8001 --device-id device-001

Requires: httpx, websockets
  pip install httpx websockets
"""

import asyncio
import json
import time
import argparse
import os
import sys

import httpx
import websockets


async def run_once(edge_base: str, device_id: str, out_dir: str = "."):
    # derive ws url
    ws_url = edge_base.replace("http://", "ws://").replace("https://", "wss://") + "/ws/updates"
    print(f"Connecting to WS: {ws_url}")

    try:
        async with websockets.connect(ws_url) as ws:
            print("WebSocket connected, waiting for model_update...")
            msg = await ws.recv()
            print("Received WS message")
            try:
                j = json.loads(msg)
            except Exception:
                print("WS message is not JSON, ignoring")
                return

            if j.get("type") != "model_update":
                print(f"WS message type={j.get('type')} not model_update, ignoring")
                return

            payload = j.get("payload", {})
            nid = payload.get("notification_id")
            model_rel = payload.get("download_rel")
            download_url = payload.get("download_url")
            model_id = payload.get("model_id") or model_rel

            if not download_url and model_rel:
                download_url = f"{edge_base}/download?rel_path={model_rel}"

            if not download_url:
                print("No download URL available in payload; aborting")
                return

            print(f"Notification id: {nid}")
            print(f"Model download URL: {download_url}")

            # Helper to post events
            def post_event(event_type: str, details: dict | None = None):
                now = time.strftime("%Y%m%d_%H%M%S_%f", time.localtime())
                body = {
                    "event_type": event_type,
                    "device_id": device_id,
                    "notification_id": nid,
                    "model_id": model_id,
                    "round": payload.get("round"),
                    "event_timestamp": now,
                    "details": details or {},
                }
                try:
                    resp = httpx.post(f"{edge_base}/device_event", json=body, timeout=10.0)
                    print(f"POST /device_event {event_type} -> {resp.status_code}")
                except Exception as e:
                    print(f"Failed to POST device_event {event_type}: {e}")

            # MODEL_LOAD_START
            post_event("MODEL_LOAD_START", {"note": "starting download and local save"})

            # download
            t0 = time.time()
            try:
                r = httpx.get(download_url, timeout=60.0)
                r.raise_for_status()
            except Exception as e:
                print(f"Download failed: {e}")
                # notify error event
                post_event("MODEL_LOAD_ERROR", {"error": str(e)})
                return

            # save file
            os.makedirs(out_dir, exist_ok=True)
            local_fp = os.path.join(out_dir, f"downloaded_{int(time.time())}.pt")
            with open(local_fp, "wb") as f:
                f.write(r.content)
            elapsed_ms = int((time.time() - t0) * 1000)

            post_event("MODEL_LOAD_END", {"download_elapsed_ms": elapsed_ms, "local_path": local_fp, "size_bytes": len(r.content)})

            # DATASET_INIT_START -> simulate small init -> DATASET_READY
            post_event("DATASET_INIT_START", {"note": "simulated dataset init"})
            await asyncio.sleep(1.0)
            post_event("DATASET_READY", {"note": "dataset ready (simulated)"})

            # TRAINING_CYCLE_START
            post_event("TRAINING_CYCLE_START", {"note": "starting local training (simulated)"})

            # final ack
            now = time.strftime("%Y%m%d_%H%M%S_%f", time.localtime())
            ack_body = {
                "device_id": device_id,
                "notification_id": nid,
                "model_id": model_id,
                "round": payload.get("round"),
                "event_timestamp": now,
            }
            try:
                resp = httpx.post(f"{edge_base}/device_ack", json=ack_body, timeout=10.0)
                print(f"POST /device_ack -> {resp.status_code} {resp.text}")
            except Exception as e:
                print(f"Failed to POST device_ack: {e}")

            print("Device test flow completed")
    except Exception as e:
        print(f"WebSocket connection failed: {e}")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--edge", required=True, help="Edge base URL, e.g. http://localhost:8001")
    p.add_argument("--device-id", default="device-001")
    p.add_argument("--out", default=".", help="Directory to save downloaded model")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(run_once(args.edge.rstrip("/"), args.device_id, args.out))
    except KeyboardInterrupt:
        print("Interrupted")
        sys.exit(0)
