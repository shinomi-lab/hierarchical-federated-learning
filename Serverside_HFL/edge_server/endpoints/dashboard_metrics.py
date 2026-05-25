from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, Any

from fastapi import APIRouter

from edge_server.state import current_edge_state, bucket_count
from edge_server.endpoints.notifications import recent_notifications
from edge_server.endpoints.notifications import notifications_manager
from edge_server.config import TERMINAL_LOGS_DIR

router = APIRouter()


def _dir_size_bytes(p: Path) -> int:
    try:
        total = 0
        if not p.exists():
            return 0
        for root, _, files in os.walk(p):
            for f in files:
                try:
                    total += Path(root, f).stat().st_size
                except Exception:
                    pass
        return total
    except Exception:
        return 0


@router.get("/api/dashboard/metrics")
def get_dashboard_metrics() -> Dict[str, Any]:
    # Round & aggregation progress
    round_id = int(current_edge_state.round)
    threshold = int(current_edge_state.aggregation_threshold)
    finished = bucket_count(round_id)
    total_clients = int(current_edge_state.clients_total or 0) if hasattr(current_edge_state, "clients_total") else finished

    # WS connections
    ws_clients = len(getattr(notifications_manager, "active_connections", []))

    # Storage usage (terminal logs)
    logs_dir = Path(TERMINAL_LOGS_DIR)
    storage_bytes = _dir_size_bytes(logs_dir)

    # Simple recent flags
    flags = {
        "waiting_for_new_model": bool(getattr(current_edge_state, "waiting_for_new_model", False)),
        "is_aggregating": bool(getattr(current_edge_state, "is_aggregating", False)),
    }

    # ACK latency stats (based on recent device_ack events)
    try:
        # We don't store latencies directly; compute from notification send times and a cached list if present
        latencies = list(getattr(current_edge_state, "recent_ack_latencies", []))
        p50 = None
        p90 = None
        if latencies:
            arr = sorted(latencies)
            def pct(p):
                idx = max(0, min(len(arr)-1, int(len(arr)*p)))
                return arr[idx]
            p50 = pct(0.5)
            p90 = pct(0.9)
    except Exception:
        p50 = None
        p90 = None

    return {
        "ts": int(time.time()),
        "round": round_id,
        "aggregation_threshold": threshold,
        "finished_updates": finished,
        "total_clients": total_clients,
        "ws_clients": ws_clients,
        "storage_bytes": storage_bytes,
        "flags": flags,
        "ack_latency_s": {"p50": p50, "p90": p90},
    }