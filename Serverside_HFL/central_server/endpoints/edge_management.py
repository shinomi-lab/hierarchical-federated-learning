# central_server/endpoints/edge_management.py

import os
import json
import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from central_server.state import edge_registry
from central_server.config import GLOBAL_MODEL_PATH, CONFIG_PATH
from pydantic import BaseModel, AnyHttpUrl
from central_server.state import edge_registry
import asyncio
import time
import random
import logging
from typing import Optional

from central_server.utils.time_logger import central_time_logger

logger = logging.getLogger("central_server.endpoints.edge_management")

# 同時に複数の push が走ることで同一 tmp パスが競合する問題（P0）を防ぐロック。
# ロック保持中に次の push リクエストが来た場合は即座にスキップする（待機しない）。
_push_lock = asyncio.Lock()


class RegisterEdgeRequest(BaseModel):
    edge_url: AnyHttpUrl

router = APIRouter()

@router.post("/send_model_and_app")
def send_model_and_app():
    """互換性のための同期ラッパー: 非同期版をブロッキング実行する。
    運用上は `async_send_model_and_app` をバックグラウンドタスクとしてスケジュールすることを推奨します。
    """
    # If we are running inside an asyncio loop, schedule background send and return quickly.
    try:
        loop = asyncio.get_running_loop()
        # schedule and return immediately; wrap the coroutine so exceptions are logged
        async def _run_and_log():
            try:
                await async_send_model_and_app()
            except Exception as e:
                # log to time_logger and standard logger so background failures are visible
                try:
                    central_time_logger.log_event("background_task_exception", {"task": "async_send_model_and_app", "error": str(e)})
                except Exception:
                    pass
                logger.exception("バックグラウンドタスク async_send_model_and_app が失敗しました")

        loop.create_task(_run_and_log())
        return JSONResponse(content={"status": "scheduled"})
    except RuntimeError:
        # No running loop: run synchronously (blocking) for compatibility
        return JSONResponse(content=asyncio.run(async_send_model_and_app()))


async def async_send_model_and_app(
    concurrency: int = 8,
    retries: int = 3,
    backoff_base: float = 0.5,
    model_bytes: Optional[bytes] = None,
    config_bytes: Optional[bytes] = None,
) -> dict:
    """非同期で登録済みエッジにモデルと設定を配布する。
    - concurrency: 同時送信数の上限
    - retries: 失敗時のリトライ回数
    - backoff_base: リトライ間の指数バックオフのベース秒数
    戻り値は edge -> status のマップ
    """
    # 前回のプッシュが完了する前に次が始まると、エッジ側で同一 tmp パスの競合（P0）が起きる。
    # ロックが取れない（既に別のプッシュ進行中）場合は即スキップする。
    if _push_lock.locked():
        logger.info("[push] 別のプッシュが進行中のためスキップします。")
        try:
            central_time_logger.log_event("push_skipped_already_in_progress", {})
        except Exception:
            pass
        return {"status": "skipped_already_in_progress"}
    async with _push_lock:
        return await _async_send_model_and_app_inner(
            concurrency=concurrency,
            retries=retries,
            backoff_base=backoff_base,
            model_bytes=model_bytes,
            config_bytes=config_bytes,
        )


async def _async_send_model_and_app_inner(
    concurrency: int = 8,
    retries: int = 3,
    backoff_base: float = 0.5,
    model_bytes: Optional[bytes] = None,
    config_bytes: Optional[bytes] = None,
) -> dict:
    if not edge_registry:
        try:
            central_time_logger.log_event("no_registered_edges", {})
        except Exception:
            pass
        logger.warning("登録済みエッジサーバがありません。プッシュをスキップします。")
        raise HTTPException(400, "No registered edge servers")

    # ファイル存在チェック
    if not os.path.exists(GLOBAL_MODEL_PATH):
        raise HTTPException(404, "Global model not found")

    # We will stream the model file to edges without reading the full config or model into memory.
    # If bytes were provided as args, they will be used; otherwise we open the file per-request below.

    results = {}
    sem = asyncio.Semaphore(concurrency)

    async def _post_to_edge(edge_url: str):
        nonlocal model_bytes, config_bytes
        attempt = 0
        while True:
            attempt += 1
            try:
                start = time.time()
                target = edge_url.rstrip('/') + '/receive_model'
                logger.debug("エッジへプッシュ準備 url=%s target=%s 試行=%d", edge_url, target, attempt)
                # choose streaming source: provided bytes or filesystem
                if model_bytes is not None:
                    import io as _io
                    mf = _io.BytesIO(model_bytes)
                    mp = {"model": (os.path.basename(GLOBAL_MODEL_PATH), mf, "application/octet-stream")}
                    # when using BytesIO, httpx will read it into memory for the request
                    stream_source = "memory"
                else:
                    mf = open(GLOBAL_MODEL_PATH, 'rb')
                    mp = {"model": (os.path.basename(GLOBAL_MODEL_PATH), mf, "application/octet-stream")}
                    stream_source = "file"
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(target, files=mp)
                if model_bytes is None:
                    try:
                        mf.close()
                    except Exception:
                        pass
                elapsed = time.time() - start
                # collect response information
                resp_status = getattr(resp, 'status_code', None)
                resp_headers = dict(getattr(resp, 'headers', {})) if hasattr(resp, 'headers') else {}
                resp_text = ""
                try:
                    raw = await resp.aread()
                    resp_text = raw[:1024].decode(errors='replace') if raw else ""
                except Exception:
                    try:
                        resp_text = (await resp.text())[:1024]
                    except Exception:
                        resp_text = "<unreadable>"

                log_payload = {
                    "edge": edge_url,
                    "target": target,
                    "status": resp_status,
                    "elapsed_s": elapsed,
                    "attempt": attempt,
                    "resp_text_trunc": resp_text,
                    "resp_headers_sample": {k: resp_headers.get(k) for k in list(resp_headers)[:5]},
                    "stream_source": stream_source,
                }
                try:
                    central_time_logger.log_event("push_to_edge_attempt", log_payload)
                except Exception:
                    pass
                logger.info("push_to_edge 結果: %s", log_payload)
                return
            except Exception as e:
                logger.exception("push_to_edge 例外 url=%s 試行=%d", edge_url, attempt)
                try:
                    central_time_logger.log_event("push_to_edge_exception", {"edge": edge_url, "attempt": attempt, "error": str(e)})
                except Exception:
                    pass
                if attempt > retries:
                    results[edge_url] = str(e)
                    try:
                        central_time_logger.log_event("push_to_edge_failed", {"edge": edge_url, "attempts": attempt, "error": str(e)})
                    except Exception:
                        pass
                    return
                # exponential backoff with jitter
                wait = backoff_base * (2 ** (attempt - 1)) * (0.8 + random.random() * 0.4)
                await asyncio.sleep(wait)

    # schedule tasks with concurrency control
    tasks = []
    for edge in list(edge_registry):
        async def _bounded(edge_url=edge):
            async with sem:
                await _post_to_edge(edge_url)
        tasks.append(asyncio.create_task(_bounded()))

    if tasks:
        await asyncio.gather(*tasks)

    return {"send_results": results, "model_path": GLOBAL_MODEL_PATH}



@router.post("/register_edge")
def register_edge(body: RegisterEdgeRequest):
    url = str(body.edge_url)
    edge_registry.add(url)              # ← set に対して add
    payload = {"edge_url": url, "edge_count": len(edge_registry)}
    try:
        central_time_logger.log_event("edge_registered", payload)
    except Exception:
        pass
    logger.info("エッジ登録 edge_registered: %s", payload)
    return {"status": "registered", "edge_count": len(edge_registry)}

@router.get("/edges")
def list_edges():
    return {"edges": sorted(edge_registry)}
