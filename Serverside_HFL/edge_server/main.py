# edge_server/main.py
from __future__ import annotations

import sys
import asyncio
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
import time
import os
from typing import Optional
import redis.asyncio as aioredis
from pydantic import BaseModel

client_states = {}
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
import time
import requests
import httpx
from datetime import datetime
import uuid
import logging

# プロジェクトのルートディレクトリをsys.pathに追加
sys.path.append(str(Path(__file__).resolve().parent.parent))

from edge_server.startup import register_startup_events, register_api_routes
from edge_server.endpoints import root, model_ops, aggregation, terminal_update, orchestration, notifications, client_logs, admin as admin_endpoints
from edge_server.background_tasks import poll_central_server_for_updates
from edge_server.logging_config import configure_logging
from edge_server.state import RetryManager, current_edge_state
from edge_server.utils.time_logger import edge_time_logger

# Initialize logger (use uvicorn.error for console visibility)
logger = logging.getLogger("uvicorn.error")
logging.basicConfig(level=logging.INFO)

configure_logging("edge_server")
retry_manager = RetryManager()

def request_round_reset():
    """中央サーバへラウンドリセットを依頼する。"""
    from edge_server.config import CENTRAL_SERVER_URL, CENTRAL_SERVER_REQUEST_TIMEOUT

    url = f"{CENTRAL_SERVER_URL.rstrip('/')}/utils/reset_round"
    try:
        response = requests.post(url, timeout=CENTRAL_SERVER_REQUEST_TIMEOUT)
        if response.ok:
            try:
                edge_time_logger.log_info("round_reset", {"status": "success"})
            except Exception:
                pass
            print("ラウンドリセットに成功しました。")
        else:
            try:
                msg = response.json().get("message", response.text)
            except Exception:
                msg = response.text
            try:
                edge_time_logger.log_warn(
                    "round_reset_failed",
                    {"status_code": response.status_code, "body": msg},
                )
            except Exception:
                pass
            print(f"ラウンドリセット失敗: status={response.status_code} body={msg}")
    except Exception as e:
        try:
            edge_time_logger.log_error("round_reset_error", {"error": repr(e)})
        except Exception:
            pass
        print(f"ラウンドリセット要求中にエラー: {e}")

def create_app() -> FastAPI:
    app = FastAPI()

    # --- Redis-backed遅延注入の設定 (マルチプロセス共有用) ---
    REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    DELAY_KEY = "experiment:delay_ms"
    CACHE_REFRESH_SEC = float(os.getenv("DELAY_CACHE_REFRESH_SEC", "1.0"))
    EXCLUDE_PATHS = (
        "/admin/config/delay",
        "/admin/topology_snapshot",
        "/ping",
        "/openapi.json",
        "/docs",
        "/redoc",
    )

    _redis: Optional[aioredis.Redis] = None
    _cached_delay_ms: int = 0
    _cache_lock = asyncio.Lock()

    async def _refresh_cache_loop():
        nonlocal _cached_delay_ms
        while True:
            try:
                if _redis is not None:
                    v = await _redis.get(DELAY_KEY)
                    if v is None:
                        val = 0
                    else:
                        try:
                            val = int(v)
                        except Exception:
                            val = 0
                    async with _cache_lock:
                        _cached_delay_ms = max(0, val)
            except Exception:
                # Redis にアクセスできない場合は現状キャッシュを維持
                pass
            await asyncio.sleep(CACHE_REFRESH_SEC)

    @app.on_event("startup")
    async def _startup_delay_redis():
        nonlocal _redis, _cached_delay_ms
        try:
            _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
            v = await _redis.get(DELAY_KEY)
            _cached_delay_ms = int(v) if v is not None else 0
        except Exception:
            _cached_delay_ms = 0
            _redis = None
        # 背景でキャッシュを更新
        asyncio.create_task(_refresh_cache_loop())

    @app.on_event("shutdown")
    async def _shutdown_delay_redis():
        nonlocal _redis
        try:
            if _redis:
                await _redis.close()
        except Exception:
            pass

    # ミドルウェア: 非同期で遅延を注入
    from starlette.middleware.base import BaseHTTPMiddleware
    class DelayMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            if any(path.startswith(p) for p in EXCLUDE_PATHS):
                return await call_next(request)
            delay_ms = _cached_delay_ms
            if delay_ms and delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000.0)
            return await call_next(request)

    app.add_middleware(DelayMiddleware)

    # --- Deterministic latency middleware based on X-Ap-Index header ---
    @app.middleware("http")
    async def simulate_deterministic_latency(request: Request, call_next):
        try:
            # M/M/1-based dynamic RTT calculation using current connected clients
            from edge_server.config import MAX_CONCURRENT_CLIENTS, EDGE_BASE_RTT_MS

            # current connected clients tracked in app.client_states (populated elsewhere)
            try:
                current_users = len(getattr(app, "client_states", {}))
            except Exception:
                current_users = 0

            capacity = int(MAX_CONCURRENT_CLIENTS or 100)
            # compute utilization rho and clip to avoid division-by-zero / explosion
            rho = float(current_users) / float(capacity) if capacity > 0 else 0.0
            max_rho = 0.95
            if rho >= max_rho:
                rho_c = max_rho
            elif rho < 0.0:
                rho_c = 0.0
            else:
                rho_c = rho

            # RTT_base is in milliseconds in config; convert to seconds for sleep
            try:
                rtt_base_ms = float(EDGE_BASE_RTT_MS)
            except Exception:
                rtt_base_ms = 20.0

            # M/M/1 dynamic RTT (ms) and convert to seconds
            try:
                rtt_dynamic_ms = rtt_base_ms / (1.0 - rho_c)
            except Exception:
                rtt_dynamic_ms = rtt_base_ms

            # Safety cap for dynamic RTT (e.g., 5s) to avoid pathological sleeps
            rtt_dynamic_sec = float(rtt_dynamic_ms) / 1000.0
            rtt_dynamic_sec = max(0.0, min(rtt_dynamic_sec, 5.0))

            mode = f"dynamic(rho={rho:.3f}->{rho_c:.3f})"
            try:
                logger.info(
                    f"ミドルウェア遅延: {mode} delay={rtt_dynamic_sec:.3f}s 利用者={current_users}/{capacity} path={request.url.path}"
                )
            except Exception:
                pass

            await asyncio.sleep(rtt_dynamic_sec)
        except Exception:
            # best-effort: do not fail request on middleware error
            pass
        response = await call_next(request)
        return response

    # 管理 API: 遅延設定の書込みと取得
    class DelayConfig(BaseModel):
        delay_ms: int

    @app.post("/admin/config/delay")
    async def set_delay(cfg: DelayConfig):
        nonlocal _redis, _cached_delay_ms
        ms = max(0, int(cfg.delay_ms))
        if _redis is None:
            # ローカルプロセスのみで反映させる
            async with _cache_lock:
                _cached_delay_ms = ms
            return {"ok": True, "delay_ms": _cached_delay_ms, "note": "redis not available, applied locally"}
        await _redis.set(DELAY_KEY, str(ms))
        async with _cache_lock:
            _cached_delay_ms = ms
        return {"ok": True, "delay_ms": _cached_delay_ms}

    @app.get("/admin/config/delay")
    async def get_delay():
        return {"delay_ms": _cached_delay_ms}

    @app.get("/ping")
    async def ping():
        return {"ts_ms": int(time.time() * 1000), "status": "ok", "delay_ms": _cached_delay_ms}

    # クライアント状態管理用の辞書（他モジュールから参照可能に）
    app.client_states = {}
    app.event_log = []  # イベント履歴
    app.websocket_notifications = {}  # 通知IDごとのACK状況
    app.round_progress = {}  # {round: {total: int, finished: int}}

    from fastapi.responses import HTMLResponse
    import json

    @app.get("/status")
    async def get_status():
        from edge_server.state import current_edge_state
        return {
            "current_round": current_edge_state.round,
            "connected_clients": len(app.client_states),
            "client_details": app.client_states,
            "event_log": app.event_log[-50:],  # 直近50件
            "websocket_notifications": app.websocket_notifications
        }

    # ダッシュボード機能は廃止

    @app.post("/notify_sent")
    async def notify_sent(notification_id: str, client_id: str):
        ws = app.websocket_notifications.setdefault(notification_id, {})
        ws[client_id] = "sent"
        app.event_log.append({
            "event": "notify_sent",
            "notification_id": notification_id,
            "client_id": client_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        return {"status": "ok"}

    @app.post("/notify_ack")
    async def notify_ack(notification_id: str, client_id: str):
        ws = app.websocket_notifications.setdefault(notification_id, {})
        ws[client_id] = "ack"
        try:
            edge_time_logger.log_info("notify_ack", {
                "notification_id": notification_id,
                "client_id": client_id,
                "timestamp": time.strftime("%Y%m%d_%H%M%S")
            })
        except Exception:
            # best-effort: do not fail the handler if logging fails
            pass

        app.event_log.append({
            "event": "notify_ack",
            "notification_id": notification_id,
            "client_id": client_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        return {"status": "ok"}

    # 全ての HTTP リクエスト／レスポンスを記録するミドルウェア
    class LoggingMiddleware(BaseHTTPMiddleware):
        def __init__(self, app: ASGIApp):
            super().__init__(app)
            self._logger = logging.getLogger("edge.request")

        async def dispatch(self, request, call_next):
            start = time.time()
            try:
                method = request.method
                path = request.url.path
                client = request.client.host if request.client else None
                # 人間が一目で判るログ行を追加: HTTP メソッドとパス
                self._logger.info(f"HTTPリクエスト: {method} {path} client={client}")
                # capture headers and short body
                try:
                    headers = {k: v for k, v in request.headers.items()}
                except Exception:
                    headers = {}
                body_preview = None
                try:
                    b = await request.body()
                    body_preview = b[:2048].decode('utf-8', errors='replace')
                except Exception:
                    body_preview = None
                self._logger.info(
                    "リクエスト開始",
                    extra={
                        "method": method,
                        "path": path,
                        "client": client,
                        "headers": dict(list(headers.items())[:20]),
                        "body_preview_len": len(body_preview) if body_preview else 0,
                    },
                )
            except Exception:
                # best-effort logging
                pass

            try:
                response = await call_next(request)
            except Exception as exc:
                # Unhandled exception in endpoint — log and re-raise
                elapsed = time.time() - start
                try:
                    self._logger.exception(
                        "request_exception（リクエスト内で未処理例外）",
                        extra={
                            "method": getattr(request, "method", None),
                            "path": getattr(request.url, "path", None),
                            "elapsed_s": elapsed,
                        },
                    )
                except Exception:
                    pass
                raise

            elapsed = time.time() - start
            try:
                status = response.status_code
                length = response.headers.get("content-length")
                self._logger.info(
                    "リクエスト終了",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "status": status,
                        "elapsed_s": elapsed,
                        "content_length": length,
                    },
                )
            except Exception:
                pass

            return response

    app.add_middleware(LoggingMiddleware)

    # --- 静的ファイル配信 (TPプローブ用など) ---
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # --- ルーター登録 ---
    app.include_router(root.router)
    app.include_router(model_ops.router)
    app.include_router(aggregation.router)
    app.include_router(terminal_update.router)
    app.include_router(client_logs.router)
    # Terminal telemetry ingestion
    try:
        from edge_server.endpoints import terminal_telemetry
        app.include_router(terminal_telemetry.router)
    except Exception:
        logger.warning("terminal_telemetry ルータを読み込めませんでした")
    # Murata-inspired network probe simulator (returns simulated RTT/TP)
    try:
        from edge_server.endpoints import network_sim
        app.include_router(network_sim.router)
    except Exception:
        logger.warning("network_sim ルータを読み込めませんでした")
    # Virtual congestion endpoint implementing Murata-style hybrid model
    try:
        from edge_server.endpoints import virtual_congestion
        app.include_router(virtual_congestion.router)
    except Exception:
        logger.warning("virtual_congestion ルータを読み込めませんでした")
    app.include_router(orchestration.router)
    # Dashboard metrics API は廃止
    # Aggregation control API
    try:
        from edge_server.endpoints import aggregation_control
        app.include_router(aggregation_control.router)
    except Exception:
        logger.warning("aggregation_control ルータを読み込めませんでした")
    app.include_router(notifications.router)

    # --- ヘルスチェック ---
    @app.get("/healthz")
    async def healthz():
        from edge_server.config import EDGE_SERVER_ID
        try:
            connected = len(getattr(app, "client_states", {}))
        except Exception:
            connected = -1
        return {
            "ok": True,
            "edge_id": EDGE_SERVER_ID,
            "round": current_edge_state.round,
            "is_aggregating": getattr(current_edge_state, "is_aggregating", False),
            "connected_clients": connected,
            "uptime_s": int(time.time() - _app_start_time),
        }

    _app_start_time = time.time()

    # @app.on_event("startup")  # 一時的に無効化（startup.pyで登録済み）
    async def startup_event_main():
        # 登録されているルートを確認（デバッグ用）
        logger.info("=== 登録済みルート一覧 ===")
        for route in app.routes:
            if hasattr(route, 'path') and hasattr(route, 'methods'):
                logger.info(f"  {route.methods} {route.path}")
            elif hasattr(route, 'path'):
                logger.info(f"  WebSocket {route.path}")
        logger.info("=== ルート一覧ここまで ===")
        
        try:
            # 中央サーバーからラウンド情報を取得して同期
            import httpx
            from edge_server.config import CENTRAL_SERVER_URL
            info_url = CENTRAL_SERVER_URL.rstrip('/') + '/api/v1/meta'
            async with httpx.AsyncClient() as client:
                resp = await client.get(info_url, timeout=3)  # タイムアウトを短縮
                if resp.status_code == 200:
                    meta = resp.json()
                    round_num = meta.get('round', 1)
                    current_edge_state.round = round_num
                    logger.info(f"エッジサーバ起動。中央からラウンド {round_num} に同期しました。")
                else:
                    current_edge_state.round = 1
                    logger.warning(f"中央からラウンド取得に失敗。1 にフォールバックします。status={resp.status_code}")
        except Exception as e:
            current_edge_state.round = 1
            logger.warning(f"中央サーバと同期できません（単独動作モード）: {e}")
        # 定期ポーリングタスクをバックグラウンドで開始（ログ付き）
        def _schedule_and_log(coro, name: Optional[str] = None):
            name = name or getattr(coro, '__name__', 'bg_task')
            logger.info("バックグラウンドタスクをスケジュールします", extra={"task": name})
            task = asyncio.create_task(coro)

            def _on_done(t):
                try:
                    exc = t.exception()
                    if exc:
                        logger.exception("bg_task_exception（バックグラウンドタスク失敗）", extra={"task": name, "error": repr(exc)})
                    else:
                        logger.info("バックグラウンドタスク完了", extra={"task": name})
                except asyncio.CancelledError:
                    logger.info("バックグラウンドタスク取消", extra={"task": name})
                except Exception:
                    logger.exception("bg_task_done_handler_error（完了コールバック失敗）", extra={"task": name})

            task.add_done_callback(_on_done)
            return task

        _schedule_and_log(poll_central_server_for_updates(), name="poll_central_server_for_updates")

    @app.on_event("shutdown")
    def shutdown_event():
        logger.info("エッジサーバを停止しました。ログファイルを閉じました。")

    # --- 起動時処理とAPIルート登録 ---
    register_startup_events(app)
    register_api_routes(app)  # /send_to_device, /download を startup.py の実装で登録
    app.include_router(admin_endpoints.router)  # /admin/graceful_reset など

    # --- エッジサーバからラウンドリセットをトリガーするエンドポイント ---
    @app.post("/trigger_reset")
    def trigger_reset():
        """Endpoint to trigger round reset from the edge server."""
        request_round_reset()
        return {"status": "reset triggered"}

    return app


if __name__ == "__main__":
    # 対話式ランチャー: エッジ名とポートを指定して起動
    import os
    import time
    import uvicorn

    def _ask(prompt: str, default: Optional[str] = None) -> str:
        try:
            v = input(f"{prompt}{' ['+default+']' if default else ''}: ").strip()
            return v or (default or "")
        except Exception:
            return default or ""

    print("=== エッジサーバ インタラクティブ起動 ===")
    print("1) edge-server-01 を 8001 で起動")
    print("2) edge-server-02 を 8002 で起動")
    print("3) カスタム設定で起動")
    choice = _ask("選択してください", "1")

    default_central = os.getenv("CENTRAL_SERVER_URL", "http://127.0.0.1:8000")
    if choice == "1":
        edge_id = "edge-server-01"
        port = "8001"
    elif choice == "2":
        edge_id = "edge-server-02"
        port = "8002"
    else:
        # カスタム入力
        default_port = os.getenv("PORT", "8001")
        default_id = os.getenv("EDGE_SERVER_ID", f"edge-server-{default_port}")
        edge_id = _ask("エッジサーバ名 (EDGE_SERVER_ID)", default_id)
        port = _ask("使用するポート番号", default_port)

    # 反映
    os.environ["EDGE_SERVER_ID"] = edge_id
    os.environ["EDGE_URL"] = f"http://127.0.0.1:{port}"
    if "CENTRAL_SERVER_URL" not in os.environ:
        os.environ["CENTRAL_SERVER_URL"] = default_central

    # 受け入れ端末数（集約閾値）を選択
    # 単一エッジ時: 1 or 2 を選択
    # 複数エッジ運用時にも各エッジごとに 1 or 2 を選択可能
    th_default = os.getenv("EDGE_AGGREGATION_THRESHOLD", "1")
    print("\n受け入れ端末数（集約閾値）を選択してください")
    print("  1) 1台の端末から受け入れ")
    print("  2) 2台の端末から受け入れ")
    th_choice = _ask("選択", th_default)
    if th_choice not in ("1", "2"):
        th_choice = th_default if th_default in ("1", "2") else "1"
    os.environ["EDGE_AGGREGATION_THRESHOLD"] = th_choice
    # 一部コードでは override を参照するため両方設定
    os.environ["EDGE_AGGREGATION_THRESHOLD_OVERRIDE"] = th_choice

    print("\n[設定]")
    print(f"  エッジID           = {edge_id}")
    print(f"  エッジURL          = http://127.0.0.1:{port}")
    print(f"  中央サーバURL      = {os.environ['CENTRAL_SERVER_URL']}")
    print(f"  ポート             = {port}")
    print(f"  受け入れ端末数(閾値) = {th_choice}")

    # ログのインスタンス分離
    try:
        from edge_server.logging_config import configure_logging as _conf_log
        _conf_log(server_name="edge_server", edge_id=edge_id)
    except Exception:
        pass

    print("\n起動します... (Ctrl+Cで停止)")
    time.sleep(0.3)
    uvicorn.run("edge_server.main:app", host="0.0.0.0", port=int(port))

app = create_app()

async def send_training_metrics_to_central_server(
    edge_id: str,
    round: int,
    accuracy=None,
    loss=None,
    app_type: str = None,
    app_index: int = None,
    satisfaction_before: float = None,
    satisfaction_after: float = None,
    terminal_id: str = None,
    tp_measured_mbps: float = None,
    rtt_measured_ms: float = None,
):
    """
    学習メトリクスを中央サーバの /upload_training_metrics へ送信する。
    CENTRAL_SERVER_URL の変更だけで同一PC・分散環境どちらにも対応。
    """
    from edge_server.config import CENTRAL_SERVER_URL
    url = f"{CENTRAL_SERVER_URL.rstrip('/')}/upload_training_metrics"
    data_id = str(uuid.uuid4())
    data = {
        "edge_id": edge_id,
        "round": round,
        "data_id": data_id,
        "event_timestamp": datetime.now().isoformat(),
    }
    if terminal_id is not None:
        data["terminal_id"] = terminal_id
    # Optional フィールドは None でなければ含める
    if accuracy is not None:
        data["accuracy"] = accuracy
    if loss is not None:
        data["loss"] = loss
    if app_type is not None:
        data["app_type"] = app_type
    if app_index is not None:
        data["app_index"] = app_index
    if satisfaction_before is not None:
        data["satisfaction_before"] = satisfaction_before
    if satisfaction_after is not None:
        data["satisfaction_after"] = satisfaction_after
    if tp_measured_mbps is not None:
        data["tp_measured_mbps"] = tp_measured_mbps
    if rtt_measured_ms is not None:
        data["rtt_measured_ms"] = rtt_measured_ms

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, data=data)
            if response.status_code == 200:
                try:
                    edge_time_logger.log_info("training_metrics_sent", {
                        "edge_id": edge_id, "round": round,
                        "app_type": app_type, "data_id": data_id,
                    })
                except Exception:
                    pass
            else:
                try:
                    edge_time_logger.log_warn("training_metrics_failed", {
                        "status_code": response.status_code, "text": response.text,
                    })
                except Exception:
                    pass
                logger.warning(f"学習メトリクスの送信に失敗しました: HTTP {response.status_code}")
    except Exception as e:
        logger.warning(f"学習メトリクス中央送信エラー: {e}")

# Example usage of retry mechanism
def send_data_to_central_server(round_id, data):
    if not retry_manager.should_retry(round_id):
        logger.warning(f"ラウンド {round_id} の再試行上限に達しました。中止します。")
        return False

    try:
        # ...existing logic to send data...
        logger.info(f"ラウンド {round_id} のデータ送信に成功しました。")
        return True
    except Exception as e:
        retry_manager.increment_retry(round_id)
        logger.info(f"ラウンド {round_id} を再試行します。現在の再試行回数: {retry_manager.retry_count[round_id]}")
        logger.error(f"ラウンド {round_id} のデータ送信に失敗。再試行回数: {retry_manager.retry_count[round_id]}")
        return False

# 注意: 二重起動を防ぐため、上記のインタラクティブランチャーのみで uvicorn.run を呼び出します。
# 直接実行時の二重 uvicorn.run セクションは削除しました。
