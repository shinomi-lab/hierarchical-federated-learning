from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, UploadFile, Request, Header
from fastapi.responses import JSONResponse

from edge_server.config import TERMINAL_LOGS_DIR, MAX_UPLOAD_SIZE, MAX_LOG_UPLOAD_SIZE_PER_FILE
from edge_server.config import LOG_RETENTION_DAYS
from edge_server.utils.time_logger import edge_time_logger
from edge_server.config import EDGE_SERVER_ID
from shared.event_logger import get_event_logger
from shared.error_responses import error_response

router = APIRouter()


def _is_multipart_file_part(value: object) -> bool:
    """Starlette / FastAPI / multipart の差で UploadFile 以外になるケースを吸収し、ファイルパートを列挙する。"""
    if value is None or isinstance(value, (str, bytes, bytearray)):
        return False
    if isinstance(value, UploadFile):
        return True
    try:
        from starlette.datastructures import UploadFile as StarletteUploadFile

        if isinstance(value, StarletteUploadFile):
            return True
    except Exception:
        pass
    read_fn = getattr(value, "read", None)
    if callable(read_fn) and hasattr(value, "filename"):
        return True
    return False


def _collect_multipart_files(form) -> List[Any]:
    files: List[Any] = []
    for key, v in form.multi_items():
        if key == "meta":
            continue
        if _is_multipart_file_part(v):
            files.append(v)
    return files


def _safe_client_filename(name: str) -> str:
    """クライアントの元ファイル名からパストラバーサルを除いたベース名のみを返す。"""
    if not name:
        return "unnamed.log"
    base = os.path.basename(str(name).replace("\\", "/"))
    base = base.replace("\x00", "").strip()
    if not base:
        return "unnamed.log"
    return base[:240]


def _sha256_streaming_path(save_path: Path, src_file: UploadFile, chunk_size: int = 1024 * 1024) -> str:
    total = 0
    h = hashlib.sha256()
    async def _write():
        nonlocal total
        import aiofiles
        async with aiofiles.open(save_path, mode="wb") as wf:
            while True:
                chunk = await src_file.read(chunk_size)
                if not chunk:
                    break
                await wf.write(chunk)
                h.update(chunk)
                total += len(chunk)
    return h, total, _write


def _normalize_sha(sha: str) -> str:
    return sha if sha.startswith("sha256:") else ("sha256:" + sha)


@router.get("/upload_client_logs/{terminal_id}/{sha256}")
async def check_log_exists(terminal_id: str, sha256: str):
    sha = _normalize_sha(sha256)
    term_dir = Path(TERMINAL_LOGS_DIR) / terminal_id
    exists = False
    try:
        # 1) 通常ログ: SHA 名のファイルを検索
        if term_dir.exists():
            for p in term_dir.glob("**/*.log"):
                name = p.stem
                if sha.endswith(name):
                    exists = True
                    break

        # 2) index.jsonl によるフルテキスト照合（エラーレポートを含む全アップロード対象）
        if not exists:
            idx_path = term_dir / "index.jsonl"
            if idx_path.exists():
                try:
                    with open(idx_path, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                entry = json.loads(line)
                                if entry.get("sha256") == sha:
                                    exists = True
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass
    except Exception:
        exists = False
    return JSONResponse(content={"exists": exists})


@router.post("/upload_client_logs/{terminal_id}")
async def upload_client_logs(
    request: Request,
    terminal_id: str,
    authorization: Optional[str] = Header(None),
):
    # Allowlist check: only permitted terminals may upload to this edge
    try:
        from edge_server.config import is_terminal_allowed, EDGE_SERVER_ID
        if not is_terminal_allowed(terminal_id):
            try:
                ev = get_event_logger("edge_client_logs", edge_id=EDGE_SERVER_ID)
                ev.warn("client_logs_reject", {"terminal_id": terminal_id, "http_status": 403, "reason": "terminal_not_allowed"})
            except Exception:
                pass
            return error_response(403, "terminal_not_allowed", "terminal not permitted on this edge")
    except Exception:
        pass

    # Authorization: Bearer <token>
    evt = None
    try:
        evt = get_event_logger("edge_client_logs", edge_id=EDGE_SERVER_ID)
        evt.info("client_logs_upload_start", {"terminal_id": terminal_id})
    except Exception:
        pass
    token = os.getenv("EDGE_UPLOAD_TOKEN")
    if token:
        if not authorization or not authorization.startswith("Bearer "):
            if evt:
                try:
                    evt.warn("auth_missing", {"terminal_id": terminal_id})
                    evt.warn("client_logs_reject", {"terminal_id": terminal_id, "http_status": 401, "reason": "auth_missing"})
                except Exception:
                    pass
            return error_response(401, "unauthorized", "Authorization ヘッダがありません")
        provided = authorization.split(" ", 1)[1].strip()
        if provided != token:
            if evt:
                try:
                    evt.warn("auth_invalid", {"terminal_id": terminal_id})
                    evt.warn("client_logs_reject", {"terminal_id": terminal_id, "http_status": 401, "reason": "auth_invalid"})
                except Exception:
                    pass
            return error_response(401, "unauthorized", "トークンが無効です")

    # Parse multipart once (avoids double-consumption of the body with File()/Form() bindings).
    try:
        form = await request.form()
    except Exception as e:
        if evt:
            try:
                evt.warn("form_parse_error", {"terminal_id": terminal_id, "error": str(e)})
                evt.warn("client_logs_reject", {"terminal_id": terminal_id, "http_status": 400, "reason": "form_parse_error"})
            except Exception:
                pass
        else:
            try:
                ev = get_event_logger("edge_client_logs", edge_id=EDGE_SERVER_ID)
                ev.warn("client_logs_reject", {"terminal_id": terminal_id, "http_status": 400, "reason": "form_parse_error"})
            except Exception:
                pass
        return error_response(400, "bad_request", "invalid or unreadable multipart body")

    meta: Optional[str] = None
    meta_val = form.get("meta")
    if isinstance(meta_val, str):
        meta = meta_val
    elif isinstance(meta_val, (bytes, bytearray)):
        try:
            meta = bytes(meta_val).decode("utf-8", errors="replace")
        except Exception:
            meta = None
    elif meta_val is not None:
        if isinstance(meta_val, UploadFile):
            try:
                meta_bytes = await meta_val.read()
                meta = meta_bytes.decode("utf-8", errors="replace")
            except Exception:
                meta = None

    # メタ JSON（先にパースして保存先・エラーレポート判定に使う）
    meta_obj: Dict[str, object] = {}
    if meta:
        try:
            meta_obj = json.loads(meta)
        except Exception:
            meta_obj = {"_invalid": True}

    is_error_report = isinstance(meta_obj, dict) and meta_obj.get("report_kind") == "error_report"

    files: List[Any] = _collect_multipart_files(form)

    if not files:
        if is_error_report:
            # ファイルなしのエラーレポートはメタのみ保存して 200 で受け付ける
            _er_batch = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1_000_000) % 1_000_000:06d}"
            _er_dir = Path(TERMINAL_LOGS_DIR) / "error_reports" / terminal_id / _er_batch
            try:
                _er_dir.mkdir(parents=True, exist_ok=True)
                (_er_dir / "README.txt").write_text(
                    "\n".join([
                        "HFL client error report (no log files attached)",
                        f"terminal_id: {terminal_id}",
                        f"edge_id: {EDGE_SERVER_ID}",
                        f"received_wall_time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                        "", "request meta (JSON):", meta if meta else "{}",
                    ]), encoding="utf-8")
                if meta:
                    (_er_dir / "request_meta.json").write_text(meta, encoding="utf-8")
            except Exception:
                pass
            if evt:
                try: evt.info(
                    "error_report_no_files_accepted",
                    {"terminal_id": terminal_id, "batch": _er_batch, "http_status": 200, "partition": "error_report"},
                )
                except Exception: pass
            return JSONResponse(content={"ack": True, "status": "no_files_received", "received": []})
        if evt:
            try:
                keys = [f"{k}:{type(v).__name__}" for k, v in form.multi_items()]
                evt.warn(
                    "no_files",
                    {
                        "terminal_id": terminal_id,
                        "http_status": 400,
                        "reason": "no_files_non_error_report",
                        "form_parts": keys[:80],
                    },
                )
            except Exception:
                try:
                    evt.warn("no_files", {"terminal_id": terminal_id, "http_status": 400})
                except Exception:
                    pass
        return error_response(400, "bad_request", "no files provided")

    # Size limits
    # overall request size hint (best-effort): use content-length header if present
    try:
        cl = request.headers.get("content-length")
        if cl and int(cl) > MAX_UPLOAD_SIZE:
            if evt:
                try: evt.warn("payload_too_large", {"terminal_id": terminal_id});
                except Exception: pass
            try:
                evt.warn("client_logs_reject", {"terminal_id": terminal_id, "http_status": 413, "reason": "payload_too_large"})
            except Exception:
                pass
            return error_response(413, "payload_too_large", "payload exceeds MAX_UPLOAD_SIZE", retry_after=60)
    except Exception:
        pass

    # 保存先ディレクトリ
    # - エラーレポート: received_files/terminal_logs/error_reports/<terminal_id>/<YYYYMMDD_HHMMSS_µs>/
    #   → 試行ごとにフォルダが分かれ、元ファイル名で保存（人間が探しやすい）
    # - 通常: received_files/terminal_logs/<terminal_id>/r<round>/<YYYYMMDD>/（従来どおり SHA 名）
    base_dir = Path(TERMINAL_LOGS_DIR) / terminal_id
    ts_day = time.strftime("%Y%m%d")
    round_label = "misc"
    error_report_batch: Optional[str] = None
    if not is_error_report:
        if isinstance(meta_obj, dict):
            r = meta_obj.get("round") or meta_obj.get("round_id") or meta_obj.get("roundNumber")
            if r is not None:
                try:
                    round_label = f"r{int(r)}"
                except Exception:
                    round_label = "misc"
        term_dir = base_dir / round_label / ts_day
    else:
        error_report_batch = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1_000_000) % 1_000_000:06d}"
        term_dir = Path(TERMINAL_LOGS_DIR) / "error_reports" / terminal_id / error_report_batch
    term_dir.mkdir(parents=True, exist_ok=True)

    if is_error_report:
        try:
            readme = term_dir / "README.txt"
            lines = [
                "HFL client error report (manual upload from device)",
                f"terminal_id: {terminal_id}",
                f"edge_id: {EDGE_SERVER_ID}",
                f"received_wall_time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"directory: {term_dir}",
                "",
                "request meta (JSON):",
                meta if meta else "{}",
            ]
            readme.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass
        try:
            if meta:
                (term_dir / "request_meta.json").write_text(meta, encoding="utf-8")
        except Exception:
            pass

    # Prune old logs by retention policy (best-effort)
    try:
        if LOG_RETENTION_DAYS > 0:
            cutoff = time.time() - LOG_RETENTION_DAYS * 86400
            # directories: terminal_id/r{round}/YYYYMMDD
            for rdir in (base_dir.glob("r*/")):
                for ddir in rdir.glob("*/"):
                    # parse date from folder name YYYYMMDD
                    try:
                        dt_str = ddir.name
                        ts_struct = time.strptime(dt_str, "%Y%m%d")
                        ts_epoch = time.mktime(ts_struct)
                        if ts_epoch < cutoff:
                            # delete old directory
                            import shutil
                            shutil.rmtree(ddir, ignore_errors=True)
                    except Exception:
                        pass
    except Exception:
        pass

    received: List[Dict[str, object]] = []
    status = "stored"
    t0 = time.time()
    skipped_empty = 0

    # meta_obj already parsed above for partitioning

    # Process each file
    for f in files:
        name = f.filename or "unknown"
        save_name = name
        # 一時ファイル名はクライアント名に依存させない（パス要素の混入を避ける）
        tmp_path = term_dir / f"_upload_{int(time.time() * 1_000_000) + len(received)}.tmp"
        hasher, total, writer = _sha256_streaming_path(tmp_path, f)
        await writer()
        if total == 0:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            skipped_empty += 1
            if evt:
                try:
                    evt.warn("empty_file_skipped", {"terminal_id": terminal_id, "name": name})
                except Exception:
                    pass
            continue
        # per-file size check
        if total > MAX_LOG_UPLOAD_SIZE_PER_FILE:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            if evt:
                try: evt.warn("file_too_large", {"terminal_id": terminal_id, "name": name, "size": total});
                except Exception: pass
            try:
                evt.warn("client_logs_reject", {"terminal_id": terminal_id, "http_status": 413, "reason": "file_too_large", "name": name})
            except Exception:
                pass
            return error_response(413, "file_too_large", f"file {name} exceeds per-file limit", retry_after=60)
        sha = "sha256:" + hasher.hexdigest()
        sha_hex = sha.split(":", 1)[1]

        if is_error_report:
            safe = _safe_client_filename(save_name)
            final_path = term_dir / safe
            if final_path.exists():
                p = Path(safe)
                stem, suf = p.stem, (p.suffix if p.suffix else ".log")
                final_path = term_dir / f"{stem}_{sha_hex[:16]}{suf}"
            try:
                tmp_path.replace(final_path)
            except Exception:
                try:
                    import shutil
                    shutil.copy2(tmp_path, final_path)
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    alt = term_dir / f"file_{sha_hex[:16]}.bin"
                    tmp_path.replace(alt)
                    final_path = alt
        else:
            # 従来: 重複検出は SHA ファイル名
            final_path = term_dir / f"{sha_hex}.log"
            if final_path.exists():
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                status = "duplicate_ignored"
                received.append({"name": name, "size": total, "sha256": sha, "stored_path": None, "duplicate": True})
                continue
            try:
                tmp_path.replace(final_path)
            except Exception:
                try:
                    import shutil
                    shutil.copy2(tmp_path, final_path)
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    alt = term_dir / _safe_client_filename(save_name)
                    tmp_path.replace(alt)
                    final_path = alt

        received.append({"name": name, "size": total, "sha256": sha, "stored_path": str(final_path.resolve())})

    if not received:
        if is_error_report:
            # ファイルがすべて空でもエラーレポートは 200 で返す（README/メタは保存済み）
            if evt:
                try: evt.info(
                    "error_report_empty_files_accepted",
                    {"terminal_id": terminal_id, "skipped_empty": skipped_empty, "http_status": 200, "partition": "error_report"},
                )
                except Exception: pass
            return JSONResponse(content={"ack": True, "status": "no_files_received", "received": []})
        if evt:
            try:
                evt.warn(
                    "no_nonempty_files",
                    {"terminal_id": terminal_id, "skipped_empty": skipped_empty, "http_status": 400, "reason": "all_empty_files"},
                )
            except Exception:
                pass
        return error_response(
            400,
            "bad_request",
            "no non-empty log files (0-byte uploads rejected)",
        )

    # Telemetry + per-terminal index JSONL
    try:
        dur_s = time.time() - t0
        edge_time_logger.log_event("client_logs_upload", {
            "terminal_id": terminal_id,
            "files": [{"name": r["name"], "size": r["size"], "sha256": r["sha256"]} for r in received],
            "status": status,
            "duration_s": dur_s,
            "meta": meta_obj,
            "edge_id": EDGE_SERVER_ID,
        })
        if evt:
            try: evt.info(
                "client_logs_upload_done",
                {
                    "terminal_id": terminal_id,
                    "count": len(received),
                    "status": status,
                    "http_status": 200,
                    "partition": "error_report" if is_error_report else "standard",
                },
            )
            except Exception: pass
        # write index.jsonl next to partition directory
        idx_path = (Path(TERMINAL_LOGS_DIR) / terminal_id / "index.jsonl")
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        with open(idx_path, "a", encoding="utf-8") as f:
            for r in received:
                sha_key = str(r["sha256"])
                sha_hex_only = sha_key.split(":", 1)[1] if ":" in sha_key else sha_key
                stored = r.get("stored_path")
                if stored:
                    path_str = str(stored)
                else:
                    path_str = str((term_dir / f"{sha_hex_only}.log").resolve())
                entry = {
                    "terminal_id": terminal_id,
                    "round": (f"error_reports/{error_report_batch}" if is_error_report and error_report_batch else round_label),
                    "date": ts_day,
                    "name": r["name"],
                    "size": r["size"],
                    "sha256": r["sha256"],
                    "path": path_str,
                    "partition": "error_report" if is_error_report else "standard",
                    "ts": int(time.time()*1000),
                    "edge_id": EDGE_SERVER_ID,
                    "run_id": (meta_obj.get("run_id") if isinstance(meta_obj, dict) else None),
                    "latency_ms": int(dur_s * 1000),
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if is_error_report and error_report_batch:
            glob_idx = Path(TERMINAL_LOGS_DIR) / "error_reports" / "incoming_batches.jsonl"
            glob_idx.parent.mkdir(parents=True, exist_ok=True)
            with open(glob_idx, "a", encoding="utf-8") as gf:
                gf.write(
                    json.dumps(
                        {
                            "terminal_id": terminal_id,
                            "batch": error_report_batch,
                            "batch_dir": str(term_dir.resolve()),
                            "edge_id": EDGE_SERVER_ID,
                            "file_count": len(received),
                            "ts_ms": int(time.time() * 1000),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    except Exception:
        pass

    return JSONResponse(content={"ack": True, "status": status, "received": received})
