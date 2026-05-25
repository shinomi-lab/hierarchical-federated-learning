from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
import os
import hashlib
import sqlite3
import json
from datetime import datetime
import asyncio
import random

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STATE_DIR = os.path.join(BASE_DIR, "state", "training_data")
FILES_DIR = os.path.join(STATE_DIR, "files")
DB_PATH = os.path.join(STATE_DIR, "metrics.db")
INDEX_PATH = os.path.join(STATE_DIR, "index.jsonl")

os.makedirs(FILES_DIR, exist_ok=True)

# Simple token for Authorization
VALID_TOKENS = {"secret-token"}

app = FastAPI()

@app.middleware("http")
async def artificial_latency_middleware(request: Request, call_next):
    try:
        # Simulate network/server processing latency between 500ms and 2000ms
        delay_s = random.uniform(0.5, 2.0)
        await asyncio.sleep(delay_s)
    except Exception:
        # If anything goes wrong, continue without blocking
        pass
    # Proceed to the next handler and attach header indicating injected delay (ms)
    response = await call_next(request)
    try:
        response.headers["X-Injected-Delay-ms"] = str(int(delay_s * 1000))
    except Exception:
        pass
    return response

class TrainingMetrics(BaseModel):
    roundNumber: int
    terminalId: Optional[str]
    epoch: int
    batchSize: int
    learningRate: float
    trainingLoss: float
    validationLoss: float
    trainingAccuracy: float
    validationAccuracy: float
    uploadSize: int
    downloadSize: int
    communicationTime: int
    cpuUsage: float
    memoryUsage: float
    batteryUsage: float
    datasetSize: int
    timestamp: int
    communicationErrors: int = 0
    trainingErrors: int = 0
    dataDistribution: Optional[Dict[str, int]] = None
    preprocessingTime: int = 0
    # Research data fields
    app_type: Optional[str] = None
    app_index: Optional[int] = None
    satisfaction_before: Optional[float] = None
    satisfaction_after: Optional[float] = None
    session_id: Optional[str] = None


def require_token(authorization: Optional[str] = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] not in VALID_TOKENS:
        raise HTTPException(status_code=403, detail="Invalid token")
    return parts[1]

# DB helper
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_number INTEGER,
        terminal_id TEXT,
        epoch INTEGER,
        batch_size INTEGER,
        learning_rate REAL,
        training_loss REAL,
        validation_loss REAL,
        training_accuracy REAL,
        validation_accuracy REAL,
        upload_size INTEGER,
        download_size INTEGER,
        communication_time INTEGER,
        cpu_usage REAL,
        memory_usage REAL,
        battery_usage REAL,
        dataset_size INTEGER,
        timestamp INTEGER,
        communication_errors INTEGER,
        training_errors INTEGER,
        data_distribution TEXT,
        preprocessing_time INTEGER,
        app_type TEXT,
        app_index INTEGER,
        satisfaction_before REAL,
        satisfaction_after REAL,
        session_id TEXT,
        created_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT,
        sha256 TEXT UNIQUE,
        device_id TEXT,
        round_number INTEGER,
        label TEXT,
        sample_type TEXT,
        timestamp INTEGER,
        size INTEGER,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

@app.post("/api/training-data")
async def post_metrics(metrics: TrainingMetrics, token: str = Depends(require_token)):
    await maybe_apply_delay()
     # insert into sqlite
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO metrics (round_number, terminal_id, epoch, batch_size, learning_rate, training_loss, validation_loss, training_accuracy, validation_accuracy, upload_size, download_size, communication_time, cpu_usage, memory_usage, battery_usage, dataset_size, timestamp, communication_errors, training_errors, data_distribution, preprocessing_time, app_type, app_index, satisfaction_before, satisfaction_after, session_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            metrics.roundNumber,
            metrics.terminalId,
            metrics.epoch,
            metrics.batchSize,
            metrics.learningRate,
            metrics.trainingLoss,
            metrics.validationLoss,
            metrics.trainingAccuracy,
            metrics.validationAccuracy,
            metrics.uploadSize,
            metrics.downloadSize,
            metrics.communicationTime,
            metrics.cpuUsage,
            metrics.memoryUsage,
            metrics.batteryUsage,
            metrics.datasetSize,
            metrics.timestamp,
            metrics.communicationErrors,
            metrics.trainingErrors,
            json.dumps(metrics.dataDistribution) if metrics.dataDistribution else None,
            metrics.preprocessingTime,
            metrics.app_type,
            metrics.app_index,
            metrics.satisfaction_before,
            metrics.satisfaction_after,
            metrics.session_id,
            datetime.utcnow().isoformat()
        )
    )
    rowid = cur.lastrowid
    conn.commit()
    conn.close()
    return JSONResponse({"status":"ok","id":rowid})

@app.post("/api/training-data/upload-sample")
async def upload_sample(
    file: UploadFile = File(...),
    device_id: str = Form(...),
    round_number: int = Form(...),
    label: Optional[str] = Form(None),
    sample_type: Optional[str] = Form(None),
    timestamp: Optional[int] = Form(None),
    token: str = Depends(require_token),
):
    await maybe_apply_delay()
    content = await file.read()
    sha = hashlib.sha256(content).hexdigest()
    filename = f"{sha}_{file.filename}"
    path = os.path.join(FILES_DIR, filename)
    # Idempotent: if sha exists, skip write
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM files WHERE sha256 = ?", (sha,))
    existing = cur.fetchone()
    if existing:
        conn.close()
        return JSONResponse({"status":"ok","sha256":sha, "filename": filename, "note":"already_exists"})
    with open(path, "wb") as fh:
        fh.write(content)
    size = len(content)
    cur.execute(
        "INSERT INTO files (filename, sha256, device_id, round_number, label, sample_type, timestamp, size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (filename, sha, device_id, round_number, label, sample_type, timestamp, size, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

    # Append to index.jsonl
    index_line = {
        "sha256": sha,
        "filename": filename,
        "device_id": device_id,
        "round_number": round_number,
        "label": label,
        "sample_type": sample_type,
        "timestamp": timestamp,
        "size": size,
        "saved_at": datetime.utcnow().isoformat()
    }
    with open(INDEX_PATH, "a", encoding="utf-8") as idx:
        idx.write(json.dumps(index_line, ensure_ascii=False) + "\n")

    return JSONResponse({"status":"ok","sha256":sha, "filename": filename})

@app.get("/api/health")
async def health():
    await maybe_apply_delay()
    return {"status":"ok"}


# Global debug delay (ms). 0 means no injected delay.
SERVER_INJECT_DELAY_MS = 0

async def maybe_apply_delay():
    """If SERVER_INJECT_DELAY_MS > 0, sleep that amount (ms) to simulate server-side processing delay."""
    try:
        ms = int(SERVER_INJECT_DELAY_MS)
        if ms > 0:
            await asyncio.sleep(ms / 1000.0)
    except Exception:
        # ignore any misconfiguration
        return


# Debug endpoints to control injected delay for testing
@app.get("/debug/delay")
async def debug_delay_once(ms: int = 0):
    """One-shot delay: sleep ms milliseconds and return header showing the injected delay."""
    if ms > 0:
        await asyncio.sleep(ms / 1000.0)
    return JSONResponse({"status":"ok","injected_ms": ms}, headers={"X-Injected-Delay-ms": str(ms)})

@app.post("/debug/delay/enable")
async def debug_delay_enable(ms: int = Form(...)):
    """Enable persistent server-side injected delay (ms) applied to all API calls via maybe_apply_delay."""
    global SERVER_INJECT_DELAY_MS
    try:
        SERVER_INJECT_DELAY_MS = int(ms)
    except Exception:
        raise HTTPException(status_code=400, detail="ms must be integer")
    return {"status":"ok","enabled_ms": SERVER_INJECT_DELAY_MS}

@app.post("/debug/delay/disable")
async def debug_delay_disable():
    global SERVER_INJECT_DELAY_MS
    SERVER_INJECT_DELAY_MS = 0
    return {"status":"ok","enabled_ms": SERVER_INJECT_DELAY_MS}
