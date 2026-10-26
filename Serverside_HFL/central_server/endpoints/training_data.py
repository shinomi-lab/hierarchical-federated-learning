from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from pydantic import BaseModel, Field
from typing import Dict, Optional
import sqlite3
import json
from pathlib import Path
import time
import logging
import hashlib
import os
from fastapi.responses import FileResponse, JSONResponse
from central_server.utils.time_logger import central_time_logger
from central_server.config import TRAINING_DATA_DIR

router = APIRouter()

DB_PATH = Path(__file__).resolve().parent.parent / "training_data.db"
INDEX_PATH = TRAINING_DATA_DIR / "index.jsonl"

logger = logging.getLogger("central_server")


class TrainingData(BaseModel):
    roundNumber: int
    terminalId: Optional[str] = None
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
    timestamp: int = Field(..., description="epoch ms")
    communicationErrors: int = 0
    trainingErrors: int = 0
    dataDistribution: Optional[Dict[str, int]] = None
    preprocessingTime: Optional[int] = 0


def _ensure_table():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Open with a short timeout and enable WAL for concurrency
    conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    try:
        cur = conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA temp_store=MEMORY")
        except Exception:
            # best-effort: if pragmas fail on some sqlite builds, continue
            pass
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS training_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id TEXT,
                round_number INTEGER,
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
                preprocessing_time INTEGER
            )
            """
        )
        # Ensure legacy DBs gain the terminal_id column if missing
        try:
            cur.execute("PRAGMA table_info(training_data)")
            cols = [r[1] for r in cur.fetchall()]
            if 'terminal_id' not in cols:
                try:
                    cur.execute("ALTER TABLE training_data ADD COLUMN terminal_id TEXT")
                    conn.commit()
                except Exception:
                    # best-effort: if alter fails, continue
                    pass
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()


@router.post("/api/training-data")
async def receive_training_data(payload: TrainingData, request: Request):
    """Receive training telemetry / metadata from clients and persist to sqlite.

    Request body must match TrainingData model. Returns JSON with inserted id.
    """
    t_start = time.time()
    central_time_logger.log_event("training_data_entry", {"remote": request.client.host if request.client else None, "round": payload.roundNumber})
    try:
        _ensure_table()
        conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
        try:
            # ensure pragmas again for this connection
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        cur = conn.cursor()

        data_dist_json = None
        if payload.dataDistribution is not None:
            # store as JSON string
            data_dist_json = json.dumps(payload.dataDistribution, ensure_ascii=False)

        # check duplicate by terminal_id + round (first-wins)
        terminal_id = getattr(payload, 'terminalId', None)
        if terminal_id:
            try:
                t_dup = time.time()
                cur.execute("SELECT id FROM training_data WHERE terminal_id = ? AND round_number = ?", (terminal_id, payload.roundNumber))
                existing = cur.fetchone()
                central_time_logger.log_event("training_data_dup_check_done", {"terminal_id": terminal_id, "round": payload.roundNumber, "found": bool(existing), "elapsed_s": time.time() - t_dup})
                if existing:
                    # already received from this terminal for this round -> ignore
                    ack_ts = int(time.time() * 1000)
                    try:
                        central_time_logger.log_event("training_data_duplicate_ignored", {"terminal_id": terminal_id, "round": payload.roundNumber, "id": existing[0]})
                    except Exception:
                        pass
                    central_time_logger.log_event("training_data_early_return", {"terminal_id": terminal_id, "round": payload.roundNumber, "reason": "duplicate"})
                    return {"status": "duplicate_ignored", "ack": True, "ack_timestamp": ack_ts}
            except Exception:
                # if duplicate check fails, proceed to insert (best-effort)
                pass

        cur.execute(
            """
            INSERT INTO training_data (
                terminal_id, round_number, epoch, batch_size, learning_rate,
                training_loss, validation_loss, training_accuracy, validation_accuracy,
                upload_size, download_size, communication_time, cpu_usage, memory_usage,
                battery_usage, dataset_size, timestamp, communication_errors, training_errors,
                data_distribution, preprocessing_time
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                terminal_id,
                payload.roundNumber,
                payload.epoch,
                payload.batchSize,
                payload.learningRate,
                payload.trainingLoss,
                payload.validationLoss,
                payload.trainingAccuracy,
                payload.validationAccuracy,
                payload.uploadSize,
                payload.downloadSize,
                payload.communicationTime,
                payload.cpuUsage,
                payload.memoryUsage,
                payload.batteryUsage,
                payload.datasetSize,
                payload.timestamp,
                payload.communicationErrors,
                payload.trainingErrors,
                data_dist_json,
                payload.preprocessingTime,
            ),
        )
        t_persist = time.time()
        conn.commit()
        rowid = cur.lastrowid
        central_time_logger.log_event("training_data_persisted", {"id": rowid, "elapsed_s": time.time() - t_persist})
        try:
            central_time_logger.log_event("training_data_received", {"id": rowid, "remote": request.client.host if request.client else None, "round": payload.roundNumber})
        except Exception:
            pass
        ack_ts = int(time.time() * 1000)
        central_time_logger.log_event("training_data_done", {"id": rowid, "total_elapsed_s": time.time() - t_start})
        return {"status": "ok", "id": rowid, "ack_timestamp": ack_ts}
    except Exception as e:
        logger.exception("教師データの永続化に失敗しました")
        # record to time logger as well
        try:
            central_time_logger.log_event("training_data_error", {"error": str(e)[:200]})
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="内部エラー")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ------------------------------------------------------------
# Raw sample upload & indexing
# ------------------------------------------------------------

def _append_index(entry: dict):
    try:
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(INDEX_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("教師データインデックスへの追記に失敗しました")


def _sha256_stream(u: UploadFile) -> str:
    h = hashlib.sha256()
    # Reset pointer if possible
    try:
        u.file.seek(0)
    except Exception:
        pass
    while True:
        chunk = u.file.read(1024 * 1024)
        if not chunk:
            break
        h.update(chunk)
    # rewind for save
    try:
        u.file.seek(0)
    except Exception:
        pass
    return h.hexdigest()


@router.post("/api/training-data/upload-sample")
async def upload_sample(
    file: UploadFile = File(...),
    device_id: str = Form(...),
    round_number: int = Form(...),
    label: Optional[str] = Form(None),
    sample_type: Optional[str] = Form(None),
    timestamp: Optional[int] = Form(None),
):
    """Accept raw training samples (images/text/etc) via multipart and persist with index.

    Stores under TRAINING_DATA_DIR/<device_id>/r<round>/<sha256>/<original_filename>
    and appends a JSONL entry to index.jsonl for later discovery.
    """
    ts0 = time.time()
    try:
        # device_id のパストラバーサル対策: パス区切り文字を含まないことを確認
        if "/" in device_id or "\\" in device_id or ".." in device_id:
            raise HTTPException(status_code=400, detail="device_id が不正です")
        sha = _sha256_stream(file)
        base_dir = (TRAINING_DATA_DIR / device_id / f"r{round_number}" / sha).resolve()
        # 保存先が TRAINING_DATA_DIR 配下にあることを確認
        if not str(base_dir).startswith(str(TRAINING_DATA_DIR.resolve())):
            logger.warning(f"アップロードでパストラバーサル疑い: device_id={device_id!r}")
            raise HTTPException(status_code=400, detail="device_id が不正です")
        base_dir.mkdir(parents=True, exist_ok=True)
        out_path = base_dir / (Path(file.filename).name or "sample.bin")
        # save file
        with open(out_path, "wb") as fw:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                fw.write(chunk)
        # manifest
        manifest = {
            "sha256": sha,
            "device_id": device_id,
            "round": int(round_number),
            "label": label,
            "sample_type": sample_type,
            "timestamp": int(timestamp) if timestamp is not None else int(time.time() * 1000),
            "filename": Path(file.filename).name,
            "path": str(out_path),
            "size_bytes": out_path.stat().st_size,
        }
        (base_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        # index append (dedup best-effort by sha+device+round)
        idx_entry = {
            "sha256": sha,
            "device_id": device_id,
            "round": int(round_number),
            "label": label,
            "sample_type": sample_type,
            "timestamp": manifest["timestamp"],
            "filename": Path(file.filename).name,
            "rel_path": os.path.relpath(out_path, TRAINING_DATA_DIR),
            "size_bytes": manifest["size_bytes"],
        }
        _append_index(idx_entry)
        try:
            central_time_logger.log_event("training_sample_uploaded", {"sha": sha, "device_id": device_id, "round": round_number})
        except Exception:
            pass
        return {"status": "ok", "sha256": sha}
    except Exception as e:
        logger.exception("サンプルのアップロードに失敗しました")
        try:
            central_time_logger.log_event("training_sample_error", {"error": str(e)[:200]})
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="内部エラー")


@router.get("/api/training-data/index")
async def list_samples(device_id: Optional[str] = None, round_number: Optional[int] = None, label: Optional[str] = None, limit: int = 100):
    """Return a filtered list of indexed samples from JSONL index."""
    res = []
    try:
        if INDEX_PATH.exists():
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        it = json.loads(line)
                    except Exception:
                        continue
                    if device_id and it.get("device_id") != device_id:
                        continue
                    if round_number is not None and int(it.get("round", -1)) != int(round_number):
                        continue
                    if label and it.get("label") != label:
                        continue
                    res.append(it)
                    if len(res) >= limit:
                        break
        return {"items": res, "count": len(res)}
    except Exception:
        logger.exception("教師データインデックスの読み取りに失敗しました")
        raise HTTPException(status_code=500, detail="内部エラー")


@router.get("/api/training-data/download/{sha}")
async def download_sample(sha: str):
    """Locate a sample by SHA and return the file (first match)."""
    try:
        # scan index for matching sha
        target_rel = None
        if INDEX_PATH.exists():
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        it = json.loads(line)
                    except Exception:
                        continue
                    if it.get("sha256") == sha:
                        target_rel = it.get("rel_path")
                        break
        if not target_rel:
            raise HTTPException(status_code=404, detail="見つかりません")
        fp = (TRAINING_DATA_DIR / target_rel).resolve()
        # Path Traversal 対策: 解決後のパスが TRAINING_DATA_DIR 配下にあることを確認
        if not str(fp).startswith(str(TRAINING_DATA_DIR.resolve())):
            logger.warning(f"パストラバーサル疑い: rel_path={target_rel!r}")
            raise HTTPException(status_code=404, detail="見つかりません")
        if not fp.exists():
            raise HTTPException(status_code=404, detail="見つかりません")
        return FileResponse(str(fp))
    except HTTPException:
        raise
    except Exception:
        logger.exception("教師データサンプルのダウンロードに失敗しました")
        raise HTTPException(status_code=500, detail="内部エラー")
