# edge_server/endpoints/aggregation.py
"""
Complete aggregation endpoint for the edge server with:
 - weighted FedAvg aggregation
 - pending-save workflow (_aggregation_cache/*.pt + *.meta.json)
 - HTTP endpoints:
    - POST /aggregate_and_push  (auto_send / interactive options)
    - GET  /pending_aggregations
    - POST /send_pending
    - POST /send_all_pending
 - background-friendly coroutine aggregate_and_send_to_central_server(...)
 - interactive terminal confirmation support (via asyncio.to_thread(input))
Notes:
 - interactive=True requires a TTY / open stdin on the process. If running under
   a daemonized server without stdin, interactive will fail (handled with error).
"""

from __future__ import annotations

import io
import os
import uuid
import asyncio
import logging
import json
import time
from pathlib import Path
from typing import List, Tuple, Dict, Iterable, Optional
import sys
import torch
import httpx
from fastapi import APIRouter, HTTPException, Body

from edge_server.state import terminal_state_dicts, state_dict_lock, UpdateRecord, clear_round, set_processing, save_state

from edge_server.config import CENTRAL_SERVER_URL, EDGE_SERVER_ID, AGGREGATION_CACHE_DIR, USE_SQLITE_PERSISTENCE, PERSISTENCE_WRITE_SIDE
from edge_server.utils.time_logger import edge_time_logger
from edge_server.utils import profiler


router = APIRouter()

# ----------------------------------------------------------------------
# Config & constants
# ----------------------------------------------------------------------
EDGE_UPDATE_PATH = "/edge_update"  # central endpoint path (no trailing slash)
SEND_RETRIES = 3
SEND_RETRY_DELAY_SEC = 5
AGGREGATION_CACHE_DIR.mkdir(exist_ok=True)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)  # ターミナルに出力
formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

# In-memory pending index (filename -> metadata)
_pending_index: Dict[str, dict] = {}
_pending_index_lock = asyncio.Lock()


# ----------------------------------------------------------------------
# FedAvg utilities (robust & validated)
# ----------------------------------------------------------------------
def _check_same_keys_and_shapes(dicts: List[dict]) -> List[str]:
    if not dicts:
        raise ValueError("empty state_dicts")
    base_keys = set(dicts[0].keys())
    for i, sd in enumerate(dicts[1:], start=1):
        if set(sd.keys()) != base_keys:
            diff_plus = set(sd.keys()) - base_keys
            diff_minus = base_keys - set(sd.keys())
            raise ValueError(f"state_dict keys mismatch at #{i}: +{sorted(list(diff_plus))}, -{sorted(list(diff_minus))}")
    for k in base_keys:
        t0 = dicts[0][k]
        shape0 = tuple(t0.size()) if hasattr(t0, "size") else None
        for i, sd in enumerate(dicts[1:], start=1):
            ti = sd[k]
            if hasattr(ti, "size") and shape0 is not None and tuple(ti.size()) != shape0:
                raise ValueError(f"shape mismatch at key='{k}': {shape0} vs {tuple(ti.size())}")
    return list(base_keys)


def _sanitize_weights(ns: Iterable[int]) -> List[float]:
    ws = [float(max(0, int(n))) for n in ns]
    s = sum(ws)
    if s <= 0:
        return [1.0 / max(1, len(ws))] * len(ws)
    return [w / s for w in ws]


def _has_nan_inf(state_dict: dict) -> bool:
    """Return True if any floating-point tensor in state_dict contains NaN or Inf."""
    for t in state_dict.values():
        if torch.is_floating_point(t):
            if torch.isnan(t).any() or torch.isinf(t).any():
                return True
    return False


def _fedavg_state_dicts_weighted(items: List[Tuple[dict, int]]) -> dict:
    """
    Weighted FedAvg:
      - float tensors: CPU float32 weighted average
      - non-float: take first's value (e.g. counters)
      - NaN/Inf を含む更新は除外する（0埋め置換は行わない）
    """
    if not items:
        raise ValueError("no items to aggregate")

    # NaN/Inf を含む更新を除外する
    valid_items = []
    for i, (sd, n) in enumerate(items):
        if _has_nan_inf(sd):
            logger.warning(f"更新 index={i} (n_samples={n}) に NaN/Inf を検出しました。集約から除外します")
            try:
                edge_time_logger.log_event("nan_in_update_excluded", {"update_idx": i, "n_samples": n})
            except Exception:
                pass
        else:
            valid_items.append((sd, n))

    if not valid_items:
        logger.error("全端末の更新が NaN/Inf を含んでいます。集約をスキップし、前回のモデルを維持します")
        try:
            edge_time_logger.log_event("aggregation_all_nan", {"original_count": len(items)})
        except Exception:
            pass
        # 最初の端末の state_dict をそのまま返す（NaN含みだが呼び出し側で処理）
        # よりも、空の state_dict を返して呼び出し側でスキップさせる
        raise ValueError("All terminal updates contained NaN/Inf; cannot aggregate")

    sds = [sd for sd, _ in valid_items]
    keys = _check_same_keys_and_shapes(sds)
    weights = _sanitize_weights(n for _, n in valid_items)
    out: Dict[str, torch.Tensor] = {}
    for k in keys:
        first = sds[0][k]
        if torch.is_floating_point(first):
            # Use an in-place weighted accumulation to reduce peak memory usage
            acc = None
            for i, sd in enumerate(sds):
                t = sd[k]
                if hasattr(t, "device") and t.device.type != "cpu":
                    t = t.detach().to("cpu")
                else:
                    t = t.detach()
                if t.dtype != torch.float32:
                    t = t.to(torch.float32)
                w = float(weights[i])
                if acc is None:
                    acc = t.mul(w)
                else:
                    acc.add_(t.mul(w))
            out[k] = acc
        else:
            # Non-floating params: bring to CPU but avoid unnecessary copy
            t0 = first
            if hasattr(t0, "device") and t0.device.type != "cpu":
                out[k] = t0.detach().to("cpu")
            else:
                out[k] = t0.detach()
    return out


# ----------------------------------------------------------------------
# Pending index helpers (persist meta + index)
# ----------------------------------------------------------------------
def _meta_path_for(filename: str) -> Path:
    return AGGREGATION_CACHE_DIR / f"{filename}.meta.json"


def _entry_path_for(filename: str) -> Path:
    return AGGREGATION_CACHE_DIR / filename


def _load_pending_index_from_disk():
    """Load any existing .meta.json at startup to rebuild in-memory index."""
    # If DB persistence is enabled and edge is not the writer, skip scanning disk
    if USE_SQLITE_PERSISTENCE and PERSISTENCE_WRITE_SIDE != 'edge':
        logger.debug("DB永続化が有効でエッジがwriterではないため、保留メタのディスク走査をスキップします")
        return
    # Recursively search for .meta.json under AGGREGATION_CACHE_DIR so that
    # run-specific subdirectories (e.g. run_<id>/rN/*.meta.json) are discovered
    # and the in-memory index correctly reflects on-disk pending entries.
    try:
        for p in AGGREGATION_CACHE_DIR.rglob("*.meta.json"):
            try:
                dd = json.loads(p.read_text(encoding="utf-8"))
                # expecting dd to contain "filename"; if not present, derive
                # a relative path from AGGREGATION_CACHE_DIR and strip the
                # trailing '.meta.json' suffix.
                fname = dd.get("filename")
                if not fname:
                    rel = p.relative_to(AGGREGATION_CACHE_DIR)
                    rel_str = str(rel).replace('\\', '/')
                    if rel_str.endswith('.meta.json'):
                        fname = rel_str[: -len('.meta.json')]
                    else:
                        fname = rel_str
                # Normalize to posix-like path string for consistent keys
                fname = str(Path(fname).as_posix())
                _pending_index[fname] = dd
            except Exception:
                logger.exception(f"保留メタの読み込みに失敗しました {p}")
    except Exception:
        logger.exception("集約キャッシュの保留メタファイル走査に失敗しました")


def _register_pending(filename: str, meta: dict):
    meta["filename"] = filename
    meta_path = _meta_path_for(filename)
    # ensure parent directory exists for meta files (handles run subdirs)
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception(f"メタパスの親ディレクトリ作成に失敗しました {meta_path}")
    # If DB persistence is enabled and edge is not configured to write persistence
    # artifacts, skip writing the meta JSON to disk to avoid conflicts.
    try:
        if not (USE_SQLITE_PERSISTENCE and PERSISTENCE_WRITE_SIDE != 'edge'):
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            logger.debug("DB永続化モードのためメタファイル書き込みをスキップします（エッジがwriterではない）")
    except Exception:
        logger.exception(f"メタ書き込みに失敗しました {filename} -> {meta_path}")
    _pending_index[filename] = meta


def _unregister_pending(filename: str):
    try:
        meta_path = _meta_path_for(filename)
        if meta_path.exists():
            meta_path.unlink()
    except Exception as e:
        logger.warning(f"メタ削除に失敗しました {filename}: {e}")
    _pending_index.pop(filename, None)


# load persisted pending on import
_load_pending_index_from_disk()


# ----------------------------------------------------------------------
# Snapshot & save helpers
# ----------------------------------------------------------------------
def _snapshot_round_nolock(round_id: int) -> Tuple[List[Tuple[dict, int]], int, int]:
    """Take a snapshot of terminal_state_dicts WITHOUT lock. Caller must hold the lock."""
    bucket: List[UpdateRecord] = terminal_state_dicts.get(round_id, [])
    if not bucket:
        raise HTTPException(status_code=400, detail=f"ラウンド {round_id} に集約する更新がありません")
    items = [(rec.state_dict, int(rec.n_samples)) for rec in bucket]
    num_clients = len(bucket)
    sum_n_samples = sum(int(rec.n_samples) for rec in bucket)
    return items, num_clients, sum_n_samples


async def _snapshot_round(round_id: int) -> Tuple[List[Tuple[dict, int]], int, int]:
    """Locking wrapper around _snapshot_round_nolock: acquires state_dict_lock then returns snapshot."""
    async with state_dict_lock:
        return _snapshot_round_nolock(round_id)


async def _save_temp_aggregated(round_id: int, state_dict: dict, model_id: Optional[str], run_id: Optional[str] = None) -> str:
    """Save aggregated state_dict into a per-run directory and create meta entry.
    Return filename (basename).
    """
    run_dir_name = f"run_{run_id}" if run_id else "run_unknown"
    dest_dir = AGGREGATION_CACHE_DIR / run_dir_name / f"r{round_id}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"agg_r{round_id}_{int(time.time())}_{uuid.uuid4().hex[:8]}.pt"
    filepath = dest_dir / filename
    tmp = filepath.with_suffix('.pt.tmp')
    try:
        # Offload the potentially expensive serialization to a thread so the
        # event loop isn't blocked (helps when aggregation is invoked from
        # an async background task).
        await asyncio.to_thread(torch.save, state_dict, tmp)
        # atomic move
        await asyncio.to_thread(tmp.replace, filepath)
    finally:
        try:
            if tmp.exists():
                await asyncio.to_thread(tmp.unlink)
        except Exception:
            pass

    # チェックサム生成（SHA256）
    import hashlib
    def _compute_sha256(path):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                h.update(chunk)
        return h.hexdigest()

    checksum = await asyncio.to_thread(_compute_sha256, filepath)

    meta = {
        "round": int(round_id),
        "run_id": run_id,
        "model_id": model_id,
        "edge_id": EDGE_SERVER_ID,
        "num_clients": None,
        "sum_n_samples": None,
        "saved_at": int(time.time()),
        "path": str(filepath.resolve()),
        "sha256": checksum,
    }
    # register pending with key that includes run directory AND round subdir (r{round})
    # this ensures the in-memory index key matches the actual on-disk location
    key = f"{run_dir_name}/r{round_id}/{filename}"
    _register_pending(key, meta)
    # Emit structured event for aggregation saved
    try:
        edge_time_logger.log_event("aggregated_saved", {
            "filename": filename,
            "round": int(round_id),
            "path": str(filepath.resolve()),
            "edge_id": EDGE_SERVER_ID,
            "run_id": run_id,
        })
    except Exception:
        pass
    return key


# ----------------------------------------------------------------------
# Norm stats helpers
# ----------------------------------------------------------------------
def _extract_norm_stats(recs) -> Optional[Dict]:
    """Extract and average normalization stats (tp/rtt mean/std) from client_meta across records."""
    tp_means, tp_stds, rtt_means, rtt_stds = [], [], [], []
    for rec in recs:
        try:
            cm = getattr(rec, "client_meta", None) or ""
            if not cm:
                continue
            jo = json.loads(cm) if isinstance(cm, str) else cm
            for lst, key in [(tp_means, "norm_tp_mean"), (tp_stds, "norm_tp_std"),
                             (rtt_means, "norm_rtt_mean"), (rtt_stds, "norm_rtt_std")]:
                v = jo.get(key)
                if v is not None:
                    lst.append(float(v))
        except Exception:
            pass
    if not tp_means or not rtt_means:
        return None
    return {
        "tp_mean":  sum(tp_means)  / len(tp_means),
        "tp_std":   max(sum(tp_stds)  / len(tp_stds),  1e-8) if tp_stds  else 1.0,
        "rtt_mean": sum(rtt_means) / len(rtt_means),
        "rtt_std":  max(sum(rtt_stds) / len(rtt_stds), 1e-8) if rtt_stds else 1.0,
        "num_clients": len(tp_means),
    }


def _persist_norm_stats(norm_stats: Dict, round_id: int) -> None:
    """Write norm_stats to AGGREGATION_CACHE_DIR/norm_stats.json (best-effort)."""
    try:
        data = dict(norm_stats)
        data["round_id"] = round_id
        data["saved_at"] = int(time.time())
        dest = AGGREGATION_CACHE_DIR / "norm_stats.json"
        dest.write_text(json.dumps(data, indent=2))
        logger.info(f"[Edge] norm_stats を保存しました: {data}")
    except Exception:
        logger.exception("[Edge] norm_stats の保存に失敗しました")


# ----------------------------------------------------------------------
# Central send helper (async, with retries)
# ----------------------------------------------------------------------
async def _post_to_central_from_path(file_path: Path, *, edge_id: str, round_id: int,
                                     model_id: Optional[str], num_clients: int, sum_n_samples: int,
                                     run_id: Optional[str] = None):
    data = {
        "edge_id": edge_id,
        "round": str(round_id),
        "num_clients": str(num_clients),
        "sum_n_samples": str(sum_n_samples),
    }
    if model_id:
        data["model_id"] = model_id
    url = f"{CENTRAL_SERVER_URL.rstrip('/')}{EDGE_UPDATE_PATH}"
    last_exc = None

    # Get current timestamp (include microseconds for uniqueness)
    from datetime import datetime
    current_timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')

    # compute content sha256 to send (normalize to central's expected format 'sha256:<hex>')
    try:
        # compute sha256 in a streaming fashion to avoid loading large files into memory
        import hashlib
        hasher = hashlib.sha256()
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                hasher.update(chunk)
        content_sha = "sha256:" + hasher.hexdigest()
    except Exception:
        content_sha = None

    # One client for all retries: reuse connection/TLS where possible (small latency win on failures)
    async with httpx.AsyncClient(timeout=120.0) as client:
        for attempt in range(SEND_RETRIES):
            try:
                logger.info(f"[Edge] 中央へ送信中 {file_path.name} 試行 {attempt+1}/{SEND_RETRIES} URL={url}")
                try:
                    edge_time_logger.log_event("send_attempt", {"filename": file_path.name, "round": int(round_id), "edge_id": edge_id, "attempt": attempt+1, "target_url": url, "content_sha256": content_sha})
                except Exception:
                    pass
                with open(file_path, "rb") as f:
                    files = {"weights": (file_path.name, f, "application/octet-stream")}
                    # include the event timestamp in the form data so central can record the same timestamp
                    data["event_timestamp"] = current_timestamp
                    # include run_id if available
                    if run_id:
                        data["run_id"] = run_id
                    # include content sha for server-side verification/idempotency
                    if content_sha:
                        data["content_sha256"] = content_sha
                    resp = await client.post(url, files=files, data=data)
                    resp.raise_for_status()

                logger.info(f"[Edge] 送信完了 {file_path.name} status={resp.status_code}")
                try:
                    edge_time_logger.log_event("sent_to_central", {"filename": file_path.name, "round": int(round_id), "edge_id": edge_id, "status": resp.status_code, "content_sha256": content_sha})
                except Exception:
                    pass
                # try parse json, but ok if no content
                try:
                    return resp.json()
                except Exception:
                    return {"status_code": resp.status_code}
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                last_exc = e
                logger.warning(f"[Edge] 送信試行 {attempt+1} が失敗しました: {e}")
                if attempt < SEND_RETRIES - 1:
                    await asyncio.sleep(SEND_RETRY_DELAY_SEC)
    raise HTTPException(status_code=502, detail=f"中央への送信が {SEND_RETRIES} 回試行後も失敗しました: {last_exc}")


# ----------------------------------------------------------------------
# Interactive prompt helper (async wrapper around blocking input)
# ----------------------------------------------------------------------
async def _ask_user_confirmation(prompt: str) -> bool:
    """
    Ask user via stdin for yes/no. Returns True for yes.
    Uses asyncio.to_thread so it won't block the event loop.
    Note: stdin must be available (TTY). If not, raises RuntimeError.
    """
    # quick check for stdin availability
    if not os.isatty(0):
        # stdin is not a TTY -> interactive not possible
        raise RuntimeError("stdin is not a TTY; interactive confirmation is not available in this process.")
    def _blocking_input() -> str:
        return input(prompt)
    try:
        res = await asyncio.to_thread(_blocking_input)
        if res is None:
            return False
        rv = res.strip().lower()
        return rv in ("y", "yes")
    except Exception as e:
        raise RuntimeError(f"interactive prompt failed: {e}")


async def _interactive_confirm_and_send(filename: str, meta: dict) -> dict:
    """Ask operator whether to send pending file; if yes, send, then cleanup and return central response."""
    prompt = f"Aggregated file '{filename}' for round={meta.get('round')} saved. Send to central now? (y/n): "
    ok = await _ask_user_confirmation(prompt)
    if not ok:
        logger.info(f"[Edge] オペレータが {filename} の送信を拒否しました")
        return {"status": "declined_by_operator", "filename": filename}
    # proceed to send
    path = AGGREGATION_CACHE_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="ディスク上に保留ファイルが見つかりません")
    central_resp = await _post_to_central_from_path(path, edge_id=EDGE_SERVER_ID,
                                                    round_id=int(meta.get("round", -1)),
                                                    model_id=meta.get("model_id"),
                                                    num_clients=int(meta.get("num_clients") or 0),
                                                    sum_n_samples=int(meta.get("sum_n_samples") or 0),
                                                    run_id=meta.get("run_id"))
    # on success: unregister pending, clear round, remove file
    async with _pending_index_lock:
        _unregister_pending(filename)
    try:
        r = int(meta.get("round"))
        async with state_dict_lock:
            clear_round(r)
    except Exception:
        logger.warning("インタラクティブ送信後にラウンドをクリアできませんでした")
    try:
        path.unlink()
    except Exception as e:
        logger.warning(f"一時ファイル削除に失敗しました {path}: {e}")
    return {"status": "sent", "filename": filename, "central_response": central_resp}


# ----------------------------------------------------------------------
# Deferred-loading helpers
# ----------------------------------------------------------------------
def _is_torch_file(path: str) -> bool:
    return str(path).lower().endswith(('.pt', '.pth'))

def _verify_file_checksum(path: str, expected_sha256: str) -> bool:
    """ファイルのSHA256チェックサムを検証する。"""
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected_sha256:
        logger.error(f"チェックサム不一致: {path} expected={expected_sha256[:16]}... actual={actual[:16]}...")
        return False
    return True


def _load_state_dict_from_file(path: str, expected_sha256: str = None):
    """Synchronous helper to load a torch state_dict from disk into CPU memory.
    Called via asyncio.to_thread to avoid blocking the event loop.
    weights_only=True は必須。legacy format は RCE リスクがあるため受け付けない。
    expected_sha256 が指定されていればロード前にチェックサム検証を行う。
    """
    if expected_sha256:
        if not _verify_file_checksum(path, expected_sha256):
            raise RuntimeError(f"チェックサム検証に失敗しました: {path}")
    try:
        sd = torch.load(str(path), map_location="cpu", weights_only=True)  # type: ignore
        return sd
    except TypeError:
        # 古い PyTorch バージョンが weights_only kwarg を受け付けない場合
        raise RuntimeError(
            "torch.load does not support weights_only kwarg; upgrade PyTorch. "
            "Legacy format files are not accepted due to RCE risk."
        )
    except Exception:
        raise

async def _ensure_state_dicts_loaded(recs: List[UpdateRecord]) -> List[UpdateRecord]:
    """Ensure each UpdateRecord in `recs` has a `state_dict`. If missing, load from `rec.path` in a thread.
    Returns the (possibly filtered) list of records where loading succeeded.
    """
    loaded = []
    for rec in recs:
        if rec.state_dict is not None:
            loaded.append(rec)
            continue
        p = rec.path
        try:
            if _is_torch_file(p):
                sd = await asyncio.to_thread(_load_state_dict_from_file, p)
                rec.state_dict = sd
                loaded.append(rec)
            else:
                # For non-torch files (e.g., .bin/.npy) we assume rec.state_dict was created at receive time.
                # If it's missing, skip this record with a log.
                logger.warning(f"メモリ上 state_dict のない非torch更新をスキップします: {p}")
        except Exception as e:
            logger.exception(f"state_dict の読み込みに失敗しました {p}: {e}")
            try:
                edge_time_logger.log_event("load_state_dict_failed", {"path": p, "error": str(e)})
            except Exception:
                pass
            # skip this rec (do not include in aggregation)
            continue
    return loaded


# ----------------------------------------------------------------------
# Public endpoint: aggregate_and_push (snapshot -> aggregate -> save pending / optional send)
# ----------------------------------------------------------------------
@router.post("/aggregate_and_push", summary="Aggregate a round and save/schedule push to central")
async def aggregate_and_push(
    round_id: int = Body(..., embed=True, description="Round ID to aggregate"),
    model_id: Optional[str] = Body(None, embed=True, description="Optional model id metadata"),
    auto_send: bool = Body(False, embed=True, description="If true, attempt immediate send to central"),
    interactive: bool = Body(False, embed=True, description="If true, ask operator via stdin to confirm sending")
):
    """
    Snapshot -> aggregate -> save aggregated weights to cache -> (optional) send to central.
    - auto_send: try to send immediately (no operator prompt)
    - interactive: ask operator via stdin; requires TTY. If interactive=True and stdin not TTY, returns error.
    """
    # 1) snapshot: take UpdateRecord objects under lock and ensure deferred loads
    try:
        async with state_dict_lock:
            bucket = list(terminal_state_dicts.get(round_id, []))
            if not bucket:
                raise HTTPException(status_code=400, detail=f"ラウンド {round_id} に集約する更新がありません")
            # clear the in-memory bucket now that we've taken a snapshot
            clear_round(round_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("aggregate_and_push のスナップショット取得で予期しないエラー")
        raise HTTPException(status_code=500, detail=f"更新のスナップショット取得に失敗しました: {e}")

    # Ensure any deferred state_dicts are loaded from disk
    loaded_recs = await _ensure_state_dicts_loaded(bucket)
    if not loaded_recs:
        raise HTTPException(status_code=400, detail=f"読み込み後、ラウンド {round_id} に有効な更新がありません")
    items = [(r.state_dict, int(r.n_samples)) for r in loaded_recs]
    num_clients = len(loaded_recs)
    sum_n_samples = sum(int(r.n_samples) for r in loaded_recs)

    # 2) aggregate (outside lock)
    agg_start_ms = int(time.time() * 1000)
    try:
        avg_sd = _fedavg_state_dicts_weighted(items)
    except Exception as e:
        logger.exception("[Edge] 集約に失敗しました")
        raise HTTPException(status_code=500, detail=f"集約に失敗しました: {e}")
    agg_duration_ms = int(time.time() * 1000) - agg_start_ms

    # 集約後の研究指標を計算してログに記録
    try:
        # 各端末の accuracy / loss 統計（client_meta JSON から抽出）
        def _extract_float(rec, key: str):
            try:
                import json as _json
                cm = getattr(rec, "client_meta", None) or getattr(rec, "manifest", None) or ""
                if cm:
                    jo = _json.loads(cm) if isinstance(cm, str) else cm
                    v = jo.get(key)
                    if v is not None:
                        return float(v)
            except Exception:
                pass
            return None
        accuracies = [v for r in loaded_recs for v in [_extract_float(r, "accuracy")] if v is not None]
        losses = [v for r in loaded_recs for v in [_extract_float(r, "loss")] if v is not None]
        # 重みの L2 ノルム（グローバルモデルのドリフト分析用）
        weight_norms = []
        for k, v in avg_sd.items():
            try:
                import torch
                if torch.is_floating_point(v):
                    weight_norms.append(float(v.norm(p=2).item()))
            except Exception:
                pass
        agg_weight_norm = sum(weight_norms) / len(weight_norms) if weight_norms else None

        run_ids = list({getattr(r, "run_id", None) for r in loaded_recs if getattr(r, "run_id", None)})

        edge_time_logger.log_event("aggregation_research_metrics", {
            "round_id": int(round_id),
            "num_clients": num_clients,
            "sum_n_samples": sum_n_samples,
            "agg_duration_ms": agg_duration_ms,
            "avg_accuracy": sum(accuracies) / len(accuracies) if accuracies else None,
            "min_accuracy": min(accuracies) if accuracies else None,
            "max_accuracy": max(accuracies) if accuracies else None,
            "avg_loss": sum(losses) / len(losses) if losses else None,
            "min_loss": min(losses) if losses else None,
            "max_loss": max(losses) if losses else None,
            "aggregated_weight_norm": agg_weight_norm,
            "participating_run_ids": run_ids,
            "edge_id": EDGE_SERVER_ID,
        })
    except Exception as e:
        logger.warning(f"集約リサーチメトリクスのログ記録に失敗しました: {e}")

    distinct_terminal_runs = sorted(
        {getattr(r, "run_id", None) for r in loaded_recs if getattr(r, "run_id", None)}
    )
    if len(distinct_terminal_runs) == 1:
        agg_run_id = distinct_terminal_runs[0]
    elif len(distinct_terminal_runs) == 0:
        agg_run_id = None
    else:
        import hashlib as _hashlib

        agg_run_id = "multi:" + _hashlib.sha256(",".join(distinct_terminal_runs).encode()).hexdigest()[:24]
        logger.warning(
            f"[Edge] aggregate_and_push: round {round_id} に複数の run_id が混在 {distinct_terminal_runs}。"
            f"中央マーカー用 run_id に合成キー {agg_run_id} を使用します"
        )

    # 3) save temp and register pending
    try:
        filename = await _save_temp_aggregated(round_id, avg_sd, model_id, run_id=agg_run_id)
        async with _pending_index_lock:
            meta = _pending_index.get(filename, {})
            meta["num_clients"] = num_clients
            meta["sum_n_samples"] = sum_n_samples
            meta["round"] = int(round_id)
            meta["run_id"] = agg_run_id
            meta["agg_duration_ms"] = agg_duration_ms
            _register_pending(filename, meta)
        logger.info(f"[Edge] 集約済みを保留として保存しました: {filename}")
        norm_stats = _extract_norm_stats(loaded_recs)
        if norm_stats:
            _persist_norm_stats(norm_stats, round_id)
    except Exception as e:
        logger.exception("[Edge] 集約重みの保存に失敗しました")
        raise HTTPException(status_code=500, detail=f"集約重みの保存に失敗しました: {e}")

    # 4) auto send
    if auto_send:
        try:
            path = AGGREGATION_CACHE_DIR / filename
            central_resp = await _post_to_central_from_path(path, edge_id=EDGE_SERVER_ID, round_id=round_id,
                                                            model_id=model_id, num_clients=num_clients, sum_n_samples=sum_n_samples,
                                                            run_id=meta.get("run_id"))
            # on success: cleanup
            async with _pending_index_lock:
                _unregister_pending(filename)
            async with state_dict_lock:
                clear_round(round_id)
            try:
                path.unlink()
            except Exception:
                logger.warning(f"一時ファイル削除に失敗しました {path}")
            return {"status": "aggregated_and_pushed", "round": round_id, "central_response": central_resp}
        except HTTPException as e:
            logger.error(f"[Edge] 自動送信に失敗しました: {e.detail}")
            return {"status": "saved_as_pending", "filename": filename, "auto_send_failed": True, "detail": str(e.detail)}

    # 5) interactive send
    if interactive:
        # interactive requires TTY
        try:
            meta = _pending_index.get(filename, {})
            resp = await _interactive_confirm_and_send(filename, meta)
            return resp
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # default: return saved pending info
    return {"status": "saved_as_pending", "filename": filename, "round": round_id, "num_clients": num_clients, "sum_n_samples": sum_n_samples}


# ----------------------------------------------------------------------
# Endpoints for operator IO: list pending, send pending, send all
# ----------------------------------------------------------------------
@router.get("/pending_aggregations", summary="List pending aggregated payloads")
async def list_pending():
    async with _pending_index_lock:
        return {"pending": list(_pending_index.values())}


@router.post("/send_pending", summary="Send a pending aggregated payload to central")
async def send_pending(filename: str = Body(..., embed=True)):
    """
    Send a single pending file (by filename) to the central server.
    Request JSON: {"filename": "agg_r1_ab12cd34.pt"}
    """
    async with _pending_index_lock:
        meta = _pending_index.get(filename)
        if not meta:
            raise HTTPException(status_code=404, detail="指定の保留ファイル名が見つかりません")
    path = AGGREGATION_CACHE_DIR / filename
    if not path.exists():
        # cleanup index
        async with _pending_index_lock:
            _unregister_pending(filename)
        raise HTTPException(status_code=404, detail="ディスク上に集約済みファイルが見つかりません")

    # attempt send
    # attempt send with reservation to avoid double-send
    rd = int(meta.get("round", -1))
    rv_run = meta.get("run_id")
    reserved = await reserve_send_round(rd, rv_run)
    if not reserved:
        logger.info(f"[Edge] send_pending: round {rd} run {rv_run} は既に予約済み/送信済み。送信をスキップします。")
        return {"status": "skipped_already_sent", "filename": filename}
    try:
        central_resp = await _post_to_central_from_path(path, edge_id=EDGE_SERVER_ID,
                                                        round_id=rd,
                                                        model_id=meta.get("model_id"),
                                                        num_clients=int(meta.get("num_clients") or 0),
                                                        sum_n_samples=int(meta.get("sum_n_samples") or 0),
                                                        run_id=rv_run)
    except Exception as e:
        # clear reservation so retry can be attempted later
        await finalize_send_failure(rd, rv_run)
        raise

    # success: finalize and clear pending
    try:
        await finalize_send_success(rd, rv_run)
    except Exception:
        logger.exception("send_pending で送信済みラウンドの記録に失敗しました")
    async with _pending_index_lock:
        _unregister_pending(filename)
    try:
        r = int(meta.get("round"))
        async with state_dict_lock:
            clear_round(r)
    except Exception:
        logger.warning("送信後にラウンドをクリアできませんでした")
    try:
        path.unlink()
    except Exception:
        logger.warning(f"一時ファイル削除に失敗しました {path}")
    return {"status": "sent", "filename": filename, "central_response": central_resp}


@router.post("/send_all_pending", summary="Send all pending aggregated payloads to central")
async def send_all_pending():
    async with _pending_index_lock:
        filenames = list(_pending_index.keys())
    results = []
    for fn in filenames:
        try:
            res = await send_pending(fn)  # reuse logic
            results.append({"filename": fn, "result": res})
        except HTTPException as e:
            results.append({"filename": fn, "error": str(e.detail)})
    return {"results": results}


@router.post("/resend_all_pending", summary="Force resend all pending aggregated payloads to central (ignore sent flags)")
async def resend_all_pending(max_batch: Optional[int] = Body(None, embed=True, description="Optional max number of pending files to resend in this call (null=all)")):
    """
    Force send all pending aggregated files to central, ignoring internal sent_rounds/set flags.
    Use this when automatic send may have failed but pending files remain on disk.
    """
    async with _pending_index_lock:
        filenames = list(_pending_index.keys())
    if max_batch:
        try:
            mb = int(max_batch)
            if mb > 0:
                filenames = filenames[:mb]
        except Exception:
            pass
    results = []
    for fn in filenames:
        meta = _pending_index.get(fn, {})
        path = AGGREGATION_CACHE_DIR / fn
        if not path.exists():
            results.append({"filename": fn, "error": "file_not_found"})
            # cleanup index if missing
            async with _pending_index_lock:
                _unregister_pending(fn)
            continue
        try:
            rd = int(meta.get("round", -1))
            rv = meta.get("run_id")
            reserved = await reserve_send_round(rd, rv)
            if not reserved:
                results.append({"filename": fn, "result": "skipped_already_sent"})
                continue
            try:
                central_resp = await _post_to_central_from_path(path, edge_id=EDGE_SERVER_ID,
                                                            round_id=rd,
                                                            model_id=meta.get("model_id"),
                                                            num_clients=int(meta.get("num_clients") or 0),
                                                            sum_n_samples=int(meta.get("sum_n_samples") or 0),
                                                            run_id=rv)
            except Exception as e:
                await finalize_send_failure(rd, rv)
                results.append({"filename": fn, "error": str(e)})
                continue

            # on success: finalize, record sent, cleanup
            try:
                await finalize_send_success(rd, rv)
            except Exception:
                logger.exception("resend_all_pending で送信済みラウンドの記録に失敗しました")
            async with _pending_index_lock:
                _unregister_pending(fn)
            try:
                path.unlink()
            except Exception:
                logger.warning(f"一時ファイル削除に失敗しました {path}")
            results.append({"filename": fn, "result": central_resp})
        except HTTPException as e:
            results.append({"filename": fn, "error": str(e.detail)})
        except Exception as e:
            results.append({"filename": fn, "error": str(e)})

    return {"results": results}


# ----------------------------------------------------------------------
# Background-friendly API (kept for compatibility)
# ----------------------------------------------------------------------
# 再送信防止のための送信済みラウンドIDを記録するセット
sent_rounds = set()
# lock for sent_rounds persistence
_sent_rounds_lock = asyncio.Lock()

# ラウンド番号を管理する変数
current_round = 1

# タイムスタンプごとにラウンドデータを保持する辞書
round_data_by_timestamp = {}

# 永続化ファイル for sent rounds
_SENT_ROUNDS_FILE = AGGREGATION_CACHE_DIR / "sent_rounds.json"

# in-memory guard for rounds currently being aggregated/sent
# use tuple (round_id, run_id) to avoid cross-run interference
_processing_rounds = set()
_processing_rounds_lock = asyncio.Lock()

# rounds that have been scheduled (to avoid scheduling the same round multiple times)
_scheduled_rounds = set()
_scheduled_rounds_lock = asyncio.Lock()

async def mark_round_scheduled(round_id: int) -> bool:
    """Return True if we successfully marked the round as scheduled, False if already scheduled."""
    async with _scheduled_rounds_lock:
        if round_id in _scheduled_rounds:
            return False
        _scheduled_rounds.add(round_id)
        return True

async def clear_round_scheduled(round_id: int) -> None:
    async with _scheduled_rounds_lock:
        _scheduled_rounds.discard(round_id)

async def is_round_scheduled(round_id: int) -> bool:
    async with _scheduled_rounds_lock:
        return round_id in _scheduled_rounds


def _load_sent_rounds_from_disk():
    """Load sent_rounds from disk if exists."""
    # If DB persistence is enabled and writes are handled elsewhere, skip file loads
    try:
        if USE_SQLITE_PERSISTENCE and PERSISTENCE_WRITE_SIDE != 'edge':
            logger.debug("DB永続化が有効で sent_rounds 書き込み設定がないためファイル読み込みをスキップします")
            return
        if _SENT_ROUNDS_FILE.exists():
            txt = _SENT_ROUNDS_FILE.read_text(encoding="utf-8")
            data = json.loads(txt)
            if isinstance(data, list):
                sent_rounds.clear()
                for r in data:
                    try:
                        sent_rounds.add(str(r))
                    except Exception:
                        pass
    except Exception:
        logger.exception("sent_rounds のディスク読み込みに失敗しました")


def _save_sent_rounds_to_disk():
    """Persist sent_rounds to disk (best-effort)."""
    try:
        if USE_SQLITE_PERSISTENCE and PERSISTENCE_WRITE_SIDE != 'edge':
            logger.debug("DB永続化が有効で sent_rounds 書き込み設定がないためファイル保存をスキップします")
            return
        # write atomically with explicit fsync to reduce risk of corruption on crash
        data = json.dumps(sorted(list(sent_rounds))).encode('utf-8')
        tmp = _SENT_ROUNDS_FILE.with_suffix('.tmp')
        import os as _os
        with open(tmp, 'wb') as fh:
            fh.write(data)
            fh.flush()
            try:
                _os.fsync(fh.fileno())
            except Exception:
                pass
        try:
            tmp.replace(_SENT_ROUNDS_FILE)
        except Exception:
            # fallback to os.replace for some platforms
            try:
                _os.replace(str(tmp), str(_SENT_ROUNDS_FILE))
            except Exception:
                raise
    except Exception:
        logger.exception("sent_rounds のディスク保存に失敗しました")


# Persistent file for in-progress sends (to avoid double-send across restarts)
_IN_PROGRESS_FILE = AGGREGATION_CACHE_DIR / "in_progress_rounds.json"

# in-memory set for in-progress sends
in_progress_rounds = set()


def _load_in_progress_from_disk():
    try:
        if USE_SQLITE_PERSISTENCE and PERSISTENCE_WRITE_SIDE != 'edge':
            logger.debug("DB永続化が有効で in_progress 書き込み設定がないためファイル読み込みをスキップします")
            return
        if _IN_PROGRESS_FILE.exists():
            txt = _IN_PROGRESS_FILE.read_text(encoding="utf-8")
            data = json.loads(txt)
            if isinstance(data, list):
                in_progress_rounds.clear()
                for r in data:
                    try:
                        in_progress_rounds.add(str(r))
                    except Exception:
                        pass
    except Exception:
        logger.exception("in_progress_rounds のディスク読み込みに失敗しました")


def _save_in_progress_to_disk():
    try:
        if USE_SQLITE_PERSISTENCE and PERSISTENCE_WRITE_SIDE != 'edge':
            logger.debug("DB永続化が有効で in_progress 書き込み設定がないためファイル保存をスキップします")
            return
        data = json.dumps(sorted(list(in_progress_rounds))).encode('utf-8')
        tmp = _IN_PROGRESS_FILE.with_suffix('.tmp')
        import os as _os
        with open(tmp, 'wb') as fh:
            fh.write(data)
            fh.flush()
            try:
                _os.fsync(fh.fileno())
            except Exception:
                pass
        try:
            tmp.replace(_IN_PROGRESS_FILE)
        except Exception:
            try:
                _os.replace(str(tmp), str(_IN_PROGRESS_FILE))
            except Exception:
                raise
    except Exception:
        logger.exception("in_progress_rounds のディスク保存に失敗しました")


async def _add_sent_round(round_id: int, run_id: Optional[str] = None):
    async with _sent_rounds_lock:
        key = f"{round_id}:{run_id or 'none'}"
        sent_rounds.add(key)
        # persist (non-blocking / best-effort)
        _save_sent_rounds_to_disk()


async def reserve_send_round(round_id: int, run_id: Optional[str] = None) -> bool:
    """Attempt to reserve the (round,run) for sending.
    Returns True if this caller has reserved the send (should proceed), False if already sent or in-progress.
    """
    async with _sent_rounds_lock:
        key = f"{round_id}:{run_id or 'none'}"
        if key in sent_rounds:
            return False
        if key in in_progress_rounds:
            return False
        in_progress_rounds.add(key)
        _save_in_progress_to_disk()
        return True


async def finalize_send_success(round_id: int, run_id: Optional[str] = None) -> None:
    async with _sent_rounds_lock:
        key = f"{round_id}:{run_id or 'none'}"
        in_progress_rounds.discard(key)
        sent_rounds.add(key)
        try:
            _save_in_progress_to_disk()
        except Exception:
            logger.exception(f"in_progress のディスク保存に失敗 (round={round_id})")
        try:
            _save_sent_rounds_to_disk()
        except Exception:
            logger.exception(f"sent_rounds のディスク保存に失敗 (round={round_id})")


async def finalize_send_failure(round_id: int, run_id: Optional[str] = None) -> None:
    async with _sent_rounds_lock:
        key = f"{round_id}:{run_id or 'none'}"
        in_progress_rounds.discard(key)
        try:
            _save_in_progress_to_disk()
        except Exception:
            logger.exception(f"in_progress のディスク保存に失敗 (round={round_id})")


# load persisted state at import
_load_sent_rounds_from_disk()

async def aggregate_and_send_to_central_server(
    edge_id: str,
    round_id: int,
    *,
    model_id: Optional[str] = None,
    auto_send: bool = False,
    interactive: bool = False
) -> None:
    global current_round
    # 集約開始: ポーリングを一時停止
    from edge_server.state import current_edge_state
    current_edge_state.is_aggregating = True
    logger.info(f"[Aggregation] ラウンド {round_id} の集約を開始しました（ポーリング一時停止）")
    
    # mark processing and persist
    try:
        set_processing(True)
        save_state(in_progress=True)
    except Exception:
        logger.exception("処理中フラグの設定に失敗しました")

    processed_rounds = set()
    try:
        # 現在のタイムスタンプを取得
        from datetime import datetime
        current_timestamp = datetime.now().isoformat()
        agg_start_ts = time.time()
        # start profiler snapshot for the whole aggregation
        try:
            _agg_token = profiler.start("aggregation_overall")
        except Exception:
            _agg_token = None

        # Log aggregation start to time logger for downstream analysis
        try:
            edge_time_logger.log_event("aggregation_started", {
                "round_id": int(round_id),
                "edge_id": edge_id,
                "model_id": model_id,
                "auto_send": bool(auto_send),
                "interactive": bool(interactive),
                "timestamp": current_timestamp,
            })
        except Exception:
            logger.exception("aggregation_started の edge_time_logger 書き込みに失敗しました")

        # 同一ラウンドが並列で処理されるのを防止するガード
        async with _processing_rounds_lock:
            # if any processing tuple exists for this round, skip
            if any(t[0] == round_id for t in _processing_rounds if isinstance(t, tuple)) or (round_id in _processing_rounds):
                logger.info(f"[Edge] ラウンド {round_id} は既に処理中です。重複集約をスキップします。")
                return
            # mark a global processing token for this round
            _processing_rounds.add((round_id, 'global'))

        # ラウンド番号の調整: 新しいラウンド番号が現在より進んでいれば進める。
        if round_id > current_round:
            current_round = round_id

        # 再送信防止チェック (同一ラウンドの全 run が既に送信済みかをざっくり確認)
        try:
            async with _sent_rounds_lock:
                sent_copy = list(sent_rounds)
        except Exception:
            sent_copy = list(sent_rounds)
        if any(s.startswith(f"{round_id}:") for s in sent_copy):
            logger.info(f"[Edge] ラウンド {round_id} はいずれかの run で既に送信済みのようです。スキップします。")
            # not a hard stop; continue to check per-run below, but avoid duplicate work

        # タイムスタンプごとにラウンドデータを記録
        round_data_by_timestamp[current_timestamp] = {
            "round_id": round_id,
            "edge_id": edge_id,
            "model_id": model_id,
            "auto_send": auto_send,
            "interactive": interactive,
        }

        current_round = round_id + 1

        # record process start in time logger
        try:
            edge_time_logger.log_info("aggregation_process", {"phase": "start", "round_id": int(round_id)})
        except Exception:
            logger.exception("集約プロセス開始のログ記録に失敗しました")

        # take an atomic snapshot of UpdateRecord objects and clear the bucket
        groups: Dict[Optional[str], List] = {}
        try:
            async with state_dict_lock:
                bucket = list(terminal_state_dicts.get(round_id, []))
                if not bucket:
                    logger.info(f"[Edge] ラウンド {round_id} に集約する更新がありません")
                    return
                # clear the in-memory bucket now that we've taken a snapshot
                clear_round(round_id)
        except Exception as e:
            logger.error(f"[Edge] 集約スナップショット中に予期しないエラー: {e}")
            return

        # run_id ごとの分割を廃止し、ラウンド単位で全端末を1グループとして集約する。
        # これにより端末ごとに別の edge_update が中央に送られる問題（P1: num_clients:1 が連続）を解消する。
        _run_ids = sorted({r.run_id for r in bucket if r.run_id})
        if len(_run_ids) == 1:
            _agg_run_key = _run_ids[0]
        elif len(_run_ids) > 1:
            import hashlib as _hashlib
            _agg_run_key = "multi:" + _hashlib.sha256(",".join(_run_ids).encode()).hexdigest()[:24]
            logger.info(
                f"[Edge] ラウンド {round_id}: 複数の run_id が混在 {_run_ids}。"
                f"合成キー {_agg_run_key} で1回の edge_update に統合します"
            )
        else:
            _agg_run_key = 'none'
        groups = {_agg_run_key: bucket}

        # For each run_id group, perform FedAvg and save/send separately
        for run_key, recs in groups.items():
            run_start_ts = time.time()
            try:
                # Ensure any deferred state_dicts are loaded from disk before aggregation.
                recs = await _ensure_state_dicts_loaded(recs)
                if not recs:
                    logger.info(f"[Edge] 読み込み後、ラウンド {round_id} run {run_key} に有効な更新がありません")
                    continue
                # profile the fedavg computation per-run
                try:
                    fed_token = profiler.start("fedavg")
                except Exception:
                    fed_token = None

                items = [(r.state_dict, int(r.n_samples)) for r in recs]
                num_clients = len(recs)
                sum_n_samples = sum(int(r.n_samples) for r in recs)
                agg_sd = _fedavg_state_dicts_weighted(items)
                # end fedavg profile
                try:
                    if fed_token is not None:
                        profiler.end(fed_token, {"round_id": round_id, "run_id": run_key, "num_clients": num_clients})
                except Exception:
                    pass
            except ValueError as ve:
                # NaN/Inf 全滅など、集約不能な場合はスキップ（前回モデル維持）
                logger.warning(f"[Edge] ラウンド {round_id} run {run_key} の集約をスキップ: {ve}")
                try:
                    edge_time_logger.log_event("aggregation_skipped_nan", {
                        "round_id": int(round_id),
                        "run_id": run_key,
                        "reason": str(ve),
                    })
                except Exception:
                    pass
                continue
            except Exception as e:
                logger.error(f"[Edge] ラウンド {round_id} run {run_key} の集約に失敗しました: {e}")
                try:
                    edge_time_logger.log_event("aggregation_failed", {
                        "round_id": int(round_id),
                        "run_id": run_key,
                        "error": str(e),
                    })
                except Exception:
                    logger.exception("aggregation_failed の edge_time_logger 書き込みに失敗しました")
                continue

            try:
                filename = await _save_temp_aggregated(round_id, agg_sd, model_id, run_id=(None if run_key == 'none' else run_key))
                async with _pending_index_lock:
                    meta = _pending_index.get(filename, {})
                    meta["num_clients"] = num_clients
                    meta["sum_n_samples"] = sum_n_samples
                    meta["round"] = int(round_id)
                    meta["run_id"] = None if run_key == 'none' else run_key
                    _register_pending(filename, meta)
                logger.info(f"✅ 集約完了。結果をファイルに保存しました: {(AGGREGATION_CACHE_DIR / filename).resolve()}")
                norm_stats = _extract_norm_stats(recs)
                if norm_stats:
                    _persist_norm_stats(norm_stats, round_id)
                # per-run aggregation complete log
                try:
                    run_dur_ms = int((time.time() - run_start_ts) * 1000)
                    edge_time_logger.log_event("aggregation_completed", {
                        "round_id": int(round_id),
                        "run_id": (None if run_key == 'none' else run_key),
                        "filename": filename,
                        "num_clients": num_clients,
                        "sum_n_samples": sum_n_samples,
                        "duration_ms": run_dur_ms,
                        "saved_path": str((AGGREGATION_CACHE_DIR / filename).resolve()),
                    })
                except Exception:
                    logger.exception("aggregation_completed の edge_time_logger 書き込みに失敗しました")

                # DEBUG: snapshot useful state to diagnose missing auto_send behavior
                try:
                    debug_pending = None
                    async with _pending_index_lock:
                        debug_pending = list(_pending_index.keys())
                except Exception:
                    debug_pending = "<failed to read pending index>"
                try:
                    sent_snapshot = None
                    async with _sent_rounds_lock:
                        sent_snapshot = list(sent_rounds)
                except Exception:
                    sent_snapshot = "<送信済みラウンドの読み取り失敗>"
                try:
                    inprog_snapshot = list(in_progress_rounds)
                except Exception:
                    inprog_snapshot = "<処理中ラウンドの読み取り失敗>"
                try:
                    logger.info(
                        f"[Edge][デバッグ] 集約完了スナップショット: round={round_id} run={run_key} filename={filename} auto_send={auto_send} pending_count={len(debug_pending) if isinstance(debug_pending, list) else 'N/A'} sent_snapshot_sample={sent_snapshot[:10] if isinstance(sent_snapshot, list) else sent_snapshot} in_progress_sample={inprog_snapshot[:10] if isinstance(inprog_snapshot, list) else inprog_snapshot} file_exists={(AGGREGATION_CACHE_DIR / filename).exists()} cache_dir={AGGREGATION_CACHE_DIR.resolve()}"
                    )
                except Exception:
                    logger.exception("集約デバッグスナップショットの出力に失敗しました")
            except Exception as e:
                logger.error(f"[Edge] ラウンド {round_id} run {run_key} の集約重み保存に失敗しました: {e}")
                continue

            key_sent = f"{round_id}:{(None if run_key == 'none' else run_key)}"
            try:
                async with _sent_rounds_lock:
                    already_sent = key_sent in sent_rounds
            except Exception:
                already_sent = key_sent in sent_rounds
            if already_sent:
                logger.info(f"[Edge] ラウンド {round_id} run {run_key} は既に送信済み。このグループの送信をスキップします。")
                continue

            if auto_send:
                logger.info(f"🚚 中央サーバへの自動送信を開始します run={run_key}...")
                path = AGGREGATION_CACHE_DIR / filename
                reserved = await reserve_send_round(round_id, None if run_key == 'none' else run_key)
                if not reserved:
                    logger.info(f"[Edge] ラウンド {round_id} run {run_key} は既に予約済みまたは送信済み。スキップします。")
                    continue
                try:
                    result = await _post_to_central_from_path(
                        path,
                        edge_id=edge_id,
                        round_id=round_id,
                        model_id=model_id,
                        num_clients=num_clients,
                        sum_n_samples=sum_n_samples,
                        run_id=(None if run_key == 'none' else run_key),
                    )
                except HTTPException as e:
                    # mark reservation cleared so retry can happen later
                    await finalize_send_failure(round_id, None if run_key == 'none' else run_key)
                    logger.error(f"❌ 自動送信に失敗しました run={run_key}: {e.detail}。ファイルは保留されます。")
                    continue
                except Exception as e:
                    await finalize_send_failure(round_id, None if run_key == 'none' else run_key)
                    logger.exception(f"❌ 自動送信で例外が発生しました run={run_key}: {e}")
                    continue

                # on success: finalize and cleanup
                try:
                    await finalize_send_success(round_id, None if run_key == 'none' else run_key)
                    try:
                        path.unlink()
                    except Exception:
                        logger.warning("送信成功後の保留ファイル削除に失敗しました。")
                    async with _pending_index_lock:
                        _unregister_pending(filename)
                    logger.info(f"✅ ラウンド {round_id} run {run_key} の集約結果を中央サーバへ送信しました。")
                    
                    # 送信成功: 新しいモデルを待機
                    from edge_server.state import current_edge_state
                    current_edge_state.waiting_for_new_model = True
                    logger.info(
                        f"[Aggregation] 中央へ送信済み。新モデル待ち (現在ラウンド: {current_edge_state.round})"
                    )
                except Exception:
                    logger.warning("送信成功後の後処理に失敗しました。")

            if interactive:
                try:
                    meta = _pending_index.get(filename, {})
                    resp = await _interactive_confirm_and_send(filename, meta)
                    if resp and resp.get("status") == "sent":
                        try:
                            await _add_sent_round(int(meta.get("round", -1)), meta.get("run_id"))
                        except Exception:
                            logger.exception("インタラクティブ送信後の送信済みラウンド記録に失敗しました")
                except Exception as e:
                    logger.warning(f"[Edge] インタラクティブ送信が失敗またはスキップされました: {e}")

        try:
            edge_time_logger.log_info("aggregation_process", {"phase": "end", "round_id": int(round_id)})
        except Exception:
            logger.exception("集約プロセス終了のログ記録に失敗しました")
        # end overall aggregation profiler
        try:
            if _agg_token is not None:
                profiler.end(_agg_token, {"round_id": round_id})
        except Exception:
            pass
    finally:
        # 処理が完了したら状態をクリア
        try:
            processed_rounds.remove(round_id)
        except Exception:
            pass
        try:
            async with _processing_rounds_lock:
                _processing_rounds.discard((round_id, 'global'))
        except Exception:
            logger.exception("処理中ラウンドガードの解除に失敗しました")
        # clear scheduled marker so future triggers can schedule again
        try:
            await clear_round_scheduled(round_id)
        except Exception:
            logger.exception("ラウンドのスケジュール済みマーカー解除に失敗しました")
        try:
            set_processing(False)
            save_state(in_progress=False)
        except Exception:
            logger.exception("処理中フラグの解除に失敗しました")
        
        # 集約処理終了: ポーリング再開
        from edge_server.state import current_edge_state
        current_edge_state.is_aggregating = False
        logger.info(f"[Aggregation] ラウンド {round_id} の集約が完了しました（ポーリング再開）")