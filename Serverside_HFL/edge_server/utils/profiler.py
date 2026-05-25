"""
Simple profiling utilities: time + memory snapshots using psutil (optional).
Logs events via `edge_server.utils.time_logger.edge_time_logger`.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

try:
    import psutil
except Exception:
    psutil = None

from edge_server.utils.time_logger import edge_time_logger


def _get_mem_rss() -> Optional[int]:
    if psutil is None:
        return None
    try:
        p = psutil.Process()
        return int(p.memory_info().rss)
    except Exception:
        return None


def start(label: str) -> Dict[str, Any]:
    """Return a token containing start timestamp and memory RSS."""
    t0 = time.time()
    mem0 = _get_mem_rss()
    return {"label": label, "t0": t0, "mem0": mem0}


def end(token: Dict[str, Any], details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Complete a profiling measurement and log via edge_time_logger.

    Returns the detail dict that was logged.
    """
    details = details or {}
    try:
        t1 = time.time()
        mem1 = _get_mem_rss()
        dur_s = t1 - float(token.get("t0", t1))
        mem0 = token.get("mem0")
        mem_delta = None
        if mem0 is not None and mem1 is not None:
            try:
                mem_delta = int(mem1) - int(mem0)
            except Exception:
                mem_delta = None
        payload = {
            "label": token.get("label"),
            "duration_s": dur_s,
            "mem_rss_before": mem0,
            "mem_rss_after": mem1,
            "mem_delta": mem_delta,
        }
        # merge user details, do not overwrite our keys
        for k, v in (details.items() if isinstance(details, dict) else []):
            if k not in payload:
                payload[k] = v
        # log event
        try:
            edge_time_logger.log_event(f"profile_{token.get('label')}", payload)
        except Exception:
            pass
        return payload
    except Exception as e:
        try:
            edge_time_logger.log_error("profile_error", {"label": token.get("label"), "error": repr(e)})
        except Exception:
            pass
        return {"label": token.get("label"), "error": repr(e)}