from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator

from edge_server.config import (
    TERMINAL_TELEMETRY_DIR,
    MAX_TELEMETRY_PAYLOAD_BYTES,
    LOG_RETENTION_DAYS,
    EDGE_SERVER_ID,
)

router = APIRouter()


class TerminalTelemetryPayload(BaseModel):
    # Required per-device spec (server will validate presence)
    timestamp_ms: Optional[int] = Field(None, description="Client event timestamp in epoch ms")
    device_id: Optional[str] = Field(None, description="Device identifier (package or device id)")
    device_model: Optional[str] = Field(None)
    device_manufacturer: Optional[str] = Field(None)
    os_sdk_int: Optional[int] = Field(None)
    network_type: Optional[str] = Field(None)

    # Optional telemetry metrics
    battery_level: Optional[float] = Field(None, description="0.0-1.0")
    wifi_ssid_masked: Optional[str] = None
    wifi_bssid_masked: Optional[str] = None
    wifi_rssi_dbm: Optional[int] = None
    wifi_link_speed: Optional[int] = None
    storage_free_mb: Optional[int] = None
    mem_used_mb: Optional[float] = None
    mem_dalvik_used_mb: Optional[float] = None
    mem_dalvik_max_mb: Optional[float] = None
    mem_native_alloc_mb: Optional[float] = None
    mem_total_mb: Optional[float] = None
    mem_free_mb: Optional[float] = None
    mem_percent: Optional[float] = None
    process_rss_mb: Optional[float] = None
    process_vms_mb: Optional[float] = None
    app_version: Optional[str] = None

    # Training metrics (optional, reported at round end)
    training_metrics_count: Optional[int] = None
    training_metrics_last_accuracy: Optional[float] = None
    training_metrics_last_loss: Optional[float] = None
    training_metrics_last_round: Optional[int] = None
    training_metrics_last: Optional[Dict[str, Any]] = None

    # Terminal satisfaction (AP selection quality, measured before/after AP switch)
    terminal_satisfaction: Optional[float] = Field(None, description="Representative satisfaction (after > before > avg)")
    terminal_satisfaction_before: Optional[float] = Field(None, description="Satisfaction before AP switch (0.0-1.0)")
    terminal_satisfaction_after: Optional[float] = Field(None, description="Satisfaction after AP switch (0.0-1.0)")
    terminal_satisfaction_delta: Optional[float] = Field(None, description="after - before")
    terminal_satisfaction_meta: Optional[Dict[str, Any]] = Field(None, description="Raw terminal_satisfaction.json snapshot")

    # Operational / auxiliary
    client_complemented: Optional[list[str]] = Field(default_factory=list)
    client_meta: Optional[str] = Field(None, description="Small JSON/string metadata")
    last_applied_round: Optional[int] = None
    current_round_local: Optional[int] = None
    training_progress: Optional[float] = None
    recent_errors: Optional[list[str]] = Field(default_factory=list)
    extras: Optional[Dict[str, Any]] = Field(default_factory=dict)

    @validator("network_type")
    def normalize_network_type(cls, v):
        if v is None:
            return v
        vv = v.upper()
        allowed = {"WIFI", "CELLULAR", "ETHERNET", "OTHER", "UNKNOWN"}
        return vv if vv in allowed else "UNKNOWN"

    @validator("training_metrics_last_accuracy", "training_metrics_last_loss", pre=True, always=False)
    def _normalize_metrics(cls, v):
        if v is None:
            return v
        try:
            fv = float(v)
        except Exception:
            raise ValueError("training metric must be numeric")
        return fv

    @validator("training_metrics_count", "training_metrics_last_round", pre=True, always=False)
    def _normalize_ints(cls, v):
        if v is None:
            return v
        try:
            return int(v)
        except Exception:
            raise ValueError("must be integer")


@router.post("/api/v1/telemetry/{terminal_id}")
@router.post("/api/telemetry/ingest/{terminal_id}")
async def ingest_terminal_telemetry(
    request: Request,
    terminal_id: str,
    payload: TerminalTelemetryPayload,
    authorization: Optional[str] = Header(None),
):
    # Optional bearer token gate, aligned with logs upload
    import os
    token = os.getenv("EDGE_UPLOAD_TOKEN")
    if token:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Authorization ヘッダがありません")
        provided = authorization.split(" ", 1)[1].strip()
        if provided != token:
            raise HTTPException(status_code=401, detail="トークンが無効です")

    # size hint via content-length
    try:
        cl = request.headers.get("content-length")
        if cl and int(cl) > MAX_TELEMETRY_PAYLOAD_BYTES:
            return JSONResponse(status_code=413, content={"ack": False, "error": "payload_too_large", "retry_after": 60}, headers={"Retry-After": "60"})
    except Exception:
        pass

    # Basic validation: record missing required fields (do not reject but annotate)
    required_fields = ["timestamp_ms", "device_id", "device_model", "os_sdk_int", "network_type"]
    # Partition by date; keep a per-terminal JSONL
    base_dir = Path(TERMINAL_TELEMETRY_DIR) / terminal_id
    ts_day = time.strftime("%Y%m%d")
    day_dir = base_dir / ts_day
    day_dir.mkdir(parents=True, exist_ok=True)

    # Retention (best-effort) — reuse LOG_RETENTION_DAYS
    try:
        if LOG_RETENTION_DAYS > 0:
            cutoff = time.time() - LOG_RETENTION_DAYS * 86400
            for ddir in base_dir.glob("*/"):
                try:
                    dt_str = ddir.name
                    ts_struct = time.strptime(dt_str, "%Y%m%d")
                    ts_epoch = time.mktime(ts_struct)
                    if ts_epoch < cutoff:
                        import shutil
                        shutil.rmtree(ddir, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass

    # Enforce client_meta size limit (8 KiB recommended)
    try:
        if payload.client_meta is not None and isinstance(payload.client_meta, str):
            b = payload.client_meta.encode("utf-8")
            if len(b) > (8 * 1024):
                payload.client_meta = b[:8 * 1024].decode("utf-8", errors="ignore")
    except Exception:
        pass

    # Append JSONL (per-terminal index)
    idx_path = base_dir / "index.jsonl"
    entry: Dict[str, Any] = payload.dict()
    entry.update({
        "terminal_id": terminal_id,
        "server_received_ts_ms": int(time.time() * 1000),
        "edge_server_id": EDGE_SERVER_ID,
        "day": ts_day,
    })
    # Fallback: if mem_used_mb is missing but dalvik/native are present, compute it
    if entry.get("mem_used_mb") is None or entry.get("mem_used_mb", -1) < 0:
        dalvik = entry.get("mem_dalvik_used_mb")
        native = entry.get("mem_native_alloc_mb")
        if isinstance(dalvik, (int, float)) and dalvik >= 0:
            total = dalvik + (native if isinstance(native, (int, float)) and native >= 0 else 0)
            entry["mem_used_mb"] = round(total, 2)

    # If training metrics are provided as nested dict, normalize and expose top-level fields
    try:
        tm = entry.get("training_metrics_last")
        if isinstance(tm, dict):
            # round -> global_round
            r = tm.get("round")
            if r is not None:
                try:
                    rr = int(r)
                    entry["global_round"] = rr
                    entry.setdefault("training_metrics_last_round", rr)
                except Exception:
                    pass

            # accuracy: accept 0-1 or 0-100, normalize to 0-1 and clamp
            try:
                acc = tm.get("accuracy")
                if acc is not None:
                    af = float(acc)
                    if af > 1.0 and af <= 100.0:
                        af = af / 100.0
                    af = max(0.0, min(1.0, af))
                    entry["training_metrics_last_accuracy"] = af
            except Exception:
                pass

            # loss: non-negative
            try:
                lf = tm.get("loss")
                if lf is not None:
                    lf_f = float(lf)
                    lf_f = max(0.0, lf_f)
                    entry["training_metrics_last_loss"] = lf_f
            except Exception:
                pass

    except Exception:
        pass

    # normalize top-level count if present
    try:
        if entry.get("training_metrics_count") is not None:
            entry["training_metrics_count"] = int(entry["training_metrics_count"])
    except Exception:
        entry["training_metrics_count"] = None
    # annotate missing fields for client visibility
    entry.setdefault("missing_fields", [])
    for f in required_fields:
        if entry.get(f) is None:
            entry["missing_fields"].append(f)

    try:
        with open(idx_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to store telemetry: {e}")

    return {"ack": True, "status": "ok", "server_ts": int(time.time() * 1000), "missing_fields": entry.get("missing_fields", [])}


@router.get("/api/v1/telemetry/query/{terminal_id}")
async def query_telemetry(terminal_id: str, limit: int = 50):
    """Admin-friendly query: returns last `limit` entries from `index.jsonl` for the terminal."""
    base_dir = Path(TERMINAL_TELEMETRY_DIR) / terminal_id
    idx_path = base_dir / "index.jsonl"
    if not idx_path.exists():
        return {"terminal_id": terminal_id, "count": 0, "entries": []}
    out = []
    try:
        with open(idx_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to read telemetry index: {e}")
    return {"terminal_id": terminal_id, "count": len(out), "entries": out}
