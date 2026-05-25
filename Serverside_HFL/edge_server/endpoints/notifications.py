from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
import asyncio
import logging
import os
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from edge_server.utils.time_logger import edge_time_logger

router = APIRouter()
log = logging.getLogger("edge.notifications")


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
        log.info(f"WebSocket クライアント接続。合計={len(self.active_connections)}")
        try:
            print(f"[Edge] WebSocket クライアント接続。合計={len(self.active_connections)}")
        except Exception:
            pass

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            try:
                self.active_connections.remove(websocket)
            except ValueError:
                pass
        log.info(f"WebSocket クライアント切断。合計={len(self.active_connections)}")
        try:
            print(f"[Edge] WebSocket クライアント切断。合計={len(self.active_connections)}")
        except Exception:
            pass

    async def send_personal(self, websocket: WebSocket, message: Dict[str, Any]):
        try:
            await websocket.send_text(json.dumps(message))
        except Exception:
            log.exception("WebSocket 個別送信に失敗しました")

    async def broadcast(self, message: Dict[str, Any]):
        # fire-and-forget per-client send to avoid one slow client blocking others
        async with self._lock:
            conns = list(self.active_connections)

        if not conns:
            log.debug("通知先の WebSocket クライアントがありません")
            try:
                print("[Edge] 通知先の WebSocket クライアントがありません")
            except Exception:
                pass
            return

        log.info(f"モデル更新を {len(conns)} クライアントへブロードキャストします")
        try:
            print(f"[Edge] モデル更新を {len(conns)} クライアントへブロードキャストします")
        except Exception:
            pass
        # schedule sends concurrently
        async def _send(ws: WebSocket):
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                log.exception("WebSocket メッセージ送信でエラー（クライアント1件）")

        await asyncio.gather(*[_send(ws) for ws in conns], return_exceptions=True)


# singleton manager
notifications_manager = ConnectionManager()

# store notification_id -> timestamp for measuring device ACK latency
recent_notifications: Dict[str, float] = {}

def _store_notification_time(notification_id: str, ts: Optional[float] = None) -> None:
    # store epoch seconds (float). If ts given use it, otherwise use current time
    if ts is None:
        ts = time.time()
    recent_notifications[notification_id] = ts
    # cleanup old entries periodically (keep last 5 minutes)
    cutoff = time.time() - 300
    keys = [k for k, v in recent_notifications.items() if v < cutoff]
    for k in keys:
        try:
            del recent_notifications[k]
        except KeyError:
            pass


async def _validate_token(token: Optional[str] = Query(None)) -> bool:
    """Simple token check for websocket clients. Use environment var WS_AUTH_TOKEN."""
    expected = os.getenv("WS_AUTH_TOKEN")
    # If no token configured, allow all (use only in trusted networks). Otherwise require match.
    if not expected:
        return True
    return token == expected


@router.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket, token: Optional[str] = Query(None)):
    # Basic auth check
    ok = await _validate_token(token)
    if not ok:
        await websocket.close(code=1008)
        return

    await notifications_manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; clients may send pings/commands if desired
            data = await websocket.receive_text()
            # echo or no-op; allow clients to request latest state
            try:
                j = json.loads(data)
            except Exception:
                j = None
            if isinstance(j, dict) and j.get("action") == "ping":
                await notifications_manager.send_personal(websocket, {"type": "pong"})
    except WebSocketDisconnect:
        await notifications_manager.disconnect(websocket)
    except Exception:
        log.exception("WebSocket エラー")
        await notifications_manager.disconnect(websocket)


async def broadcast_model_update(metadata: Dict[str, Any]) -> str:
    """Broadcast a unified `model_update` payload.

    Payload fields under `payload`:
      - schema_version: int
      - notification_id: str (UUID)
      - round: Optional[int]
      - download_rel: Optional[str]
      - download_url: Optional[str]
      - description: Optional[str]
      - timestamp: str (YYYYMMDD_HHMMSS)
    """
    nid = str(uuid.uuid4())
    now_str = time.strftime("%Y%m%d_%H%M%S")
    # configurable delay hint to smooth downloads across many devices
    try:
        import edge_server.config as cfg
        delay_hint_ms = int(getattr(cfg, "WS_DELAY_HINT_MS", 0))
    except Exception:
        delay_hint_ms = 0

    m = {
        "schema_version": 1,
        "notification_id": nid,
        "round": metadata.get("round"),
        # Provide explicit relative paths for model binary and metadata when available.
        # Callers MUST NOT include a .pt path here; if binary is not present they should
        # refrain from broadcasting model_update.
        "model_bin_rel": metadata.get("model_bin_rel"),
        "model_meta_rel": metadata.get("model_meta_rel"),
        "download_url": metadata.get("download_url"),
        "description": metadata.get("description"),
        "timestamp": metadata.get("timestamp") or now_str,
        "delay_hint_ms": delay_hint_ms,
    }
    try:
        edge_time_logger.log_event("notify_devices", m)
    except Exception:
        pass

    try:
        print(f"[Edge] 端末通知ペイロード: notification_id={nid} round={m.get('round')} model_bin_rel={m.get('model_bin_rel')} model_meta_rel={m.get('model_meta_rel')}")
    except Exception:
        pass

    send_ts = time.time()
    try:
        edge_time_logger.log_event("notification_sent", {"notification_id": nid, "send_ts": send_ts})
    except Exception:
        pass
    try:
        print(f"[Edge] 通知送信済み id={nid} send_ts={send_ts}")
    except Exception:
        pass
    _store_notification_time(nid, send_ts)

    await notifications_manager.broadcast({"type": "model_update", "payload": m})
    try:
        print(f"[Edge] ブロードキャスト完了 notification_id={nid}")
    except Exception:
        pass
    return nid
