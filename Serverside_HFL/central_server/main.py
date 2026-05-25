# central_server/main.py
import sys
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse as FastAPIJSONResponse
from fastapi.responses import JSONResponse
import traceback
import logging
import os
import json
from datetime import datetime
# Prefer package-relative import when run as a module, but allow direct script
# execution (e.g. `python central_server/main.py`) by falling back to an
# absolute import or updating sys.path.
try:
    from .logging_config import configure_logging
except Exception:
    try:
        from logging_config import configure_logging
    except Exception:
        # As a last resort, add project root to sys.path and retry.
        sys.path.append(str(Path(__file__).resolve().parent.parent))
        from logging_config import configure_logging
from uuid import uuid4
from pydantic import BaseModel
from typing import Optional

# ルートディレクトリをsys.pathに追加
sys.path.append(str(Path(__file__).resolve().parent.parent))

# ログ設定を初期化
configure_logging("central_server")

# Generate a new log file name with a timestamp
def get_log_file_name():
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    # ensure directory exists
    p = Path(__file__).resolve().parent.parent / 'logs' / 'time_records'
    p.mkdir(parents=True, exist_ok=True)
    return str(p / f'central_server_{timestamp}.log')

# Configure logging to use the new file name BEFORE importing endpoints
log_file = get_log_file_name()
logging.basicConfig(
    filename=log_file,
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True,
    encoding="utf-8",
)

logging.info("中央サーバ: セッション用ログファイルを初期化しました。")

# ログファイルの設定
log_filename = f"logs/time_records/central_server_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.DEBUG,
    filename=log_filename,
    filemode="w",
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)

# ログ開始メッセージ
logging.info("中央サーバ: 起動しました（新しいログファイル）。")

# Ensure the central_time_logger (used by endpoints) writes to the same session file
try:
    from central_server.utils.time_logger import central_time_logger
    # prefer absolute path
    central_time_logger.session_file = Path(log_file).resolve()
    # set current_session to match filename suffix after 'central_server_'
    stem = Path(log_file).stem
    if stem.startswith('central_server_'):
        central_time_logger.current_session = stem.replace('central_server_', '')
    # write an initial time-record entry so the session file exists and uses the same format
    try:
        central_time_logger.log_event("central_started", {"message": "中央サーバ起動", "pid": os.getpid()})
    except Exception:
        logging.warning("central_time_logger はあるが起動イベントの書き込みに失敗しました")
except Exception:
    logging.warning("central_time_logger のセッションファイルをアプリログと同期できませんでした")

from central_server.startup import register_startup_events
from central_server.endpoints import root, edge_management, global_model, download
from central_server.endpoints import edge_update
from central_server.endpoints import rounds
from central_server.endpoints import training_data
from central_server.endpoints import markers
from central_server.endpoints import edge_aggregation
from central_server.endpoints import ops_topology
from central_server.endpoints import trials
from central_server.state import RoundState

# `interactive` endpoint is optional; import if present to avoid startup ImportError
interactive = None
try:
    # This may be provided in some deployments; keep optional to avoid crash when absent
    from central_server.endpoints import interactive as _interactive
    interactive = _interactive
except Exception:
    logging.info("オプションのエンドポイント interactive は無し。このまま続行します。")

def create_app() -> FastAPI:
    app = FastAPI()

    # ASGI middleware: record end-to-end request timing for every HTTP request
    @app.middleware("http")
    async def request_timing_middleware(request: Request, call_next):
        import time
        start = time.time()
        method = request.method
        path = request.url.path
        content_length = request.headers.get('content-length')

        # Read and log request headers + truncated body (best-effort)
        try:
            headers = {k: v for k, v in request.headers.items()}
        except Exception:
            headers = {}

        body_preview = None
        try:
            body = await request.body()
            if body is not None:
                # limit to 2KB for logs
                body_preview = body[:2048].decode('utf-8', errors='replace')
        except Exception:
            body_preview = None

        try:
            central_time_logger.log_event("request_start", {"method": method, "path": path, "headers": headers, "body_preview": body_preview, "content_length": content_length})
        except Exception:
            logging.debug("request_start を central_time_logger に書けませんでした")

        # Re-create request for downstream with the same body
        async def receive_gen(body_bytes: bytes):
            more_body = False
            async def _receive():
                return {"type": "http.request", "body": body_bytes, "more_body": more_body}
            return _receive

        req = Request(request.scope, await receive_gen(body if 'body' in locals() else b""))

        try:
            resp = await call_next(req)
            status_code = resp.status_code
        except Exception as e:
            end = time.time()
            duration = end - start
            payload = {"method": method, "path": path, "status_code": 500, "content_length": content_length, "duration_s": duration}
            try:
                central_time_logger.log_event("request_e2e", payload)
            except Exception:
                logging.debug("request_e2e を central_time_logger に書けませんでした")
            logging.exception(f"リクエスト処理中の例外 {method} {path}: {e}")
            raise

        end = time.time()
        duration = end - start
        payload = {"method": method, "path": path, "status_code": int(status_code), "content_length": content_length, "duration_s": duration}
        try:
            central_time_logger.log_event("request_e2e", payload)
        except Exception:
            logging.debug("request_e2e を central_time_logger に書けませんでした")

        logging.info(
            f"request_e2e: {payload} headers={dict(list(headers.items())[:20])} body_preview_len={len(body_preview) if body_preview else 0}"
        )
        return resp

    # --- 500 を必ず JSON で返すグローバル例外ハンドラ（診断用） ---
    @app.exception_handler(Exception)
    async def any_exception_handler(request: Request, exc: Exception):
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        # ※本番では traceback を返さない運用を推奨。今は原因特定のため返す。
        return JSONResponse(
            status_code=500,
            content={
                "detail": "internal_error",
                "error": str(exc),
                "path": str(request.url),
                "traceback": tb[:4000],  # 長すぎ防止
            },
        )

    # バリデーションエラーは 400(Bad Request) として返す（端末の要求仕様に合わせる）
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # exc.errors() を短くして返す
        errors = exc.errors()
        short = []
        for e in errors:
            short.append({
                "loc": e.get("loc"),
                "msg": e.get("msg"),
                "type": e.get("type"),
            })
        # ログにも残す
        logging.warning("バリデーションエラー path=%s: %s", request.url.path, short)
        try:
            central_time_logger.log_event("validation_error", {"path": str(request.url.path), "errors": short})
        except Exception:
            pass
        return FastAPIJSONResponse(status_code=400, content={"detail": "validation_error", "errors": short})

    # ルーター登録
    app.include_router(root.router)               # 例: / や /status など
    app.include_router(edge_management.router)    # 例: /register_edge など
    app.include_router(ops_topology.router)     # /ops/edge-terminal-topology 運用ビュー

    # ⚠️ /get_global_model をどのモジュールが提供するかは“1箇所に統一”
    #   すでに download.py に get_global_model を置いたなら、global_model.router が
    #   同じパスを定義していないか確認。重複していると混乱のもと（実装差で500化）。
    app.include_router(download.router)           # 例: /get_global_model, /download_*
    # app.include_router(global_model.router)     # ← 重複ならコメントアウト or 中身を統一

    app.include_router(edge_update.router)
    # markers provides light-weight marker check APIs used by terminals/edges
    app.include_router(markers.router)
    app.include_router(rounds.router)
    app.include_router(training_data.router)
    app.include_router(edge_aggregation.router)
    app.include_router(trials.router)
    # include optional interactive router only when module was imported successfully
    if interactive is not None:
        try:
            app.include_router(interactive.router)
        except Exception:
            logging.warning("interactive モジュールはあるがルータ登録に失敗。スキップします。")

    # ヘルスチェック
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # --- 管理: 学習状態の完全リセット ---
    @app.post("/admin/graceful_reset")
    async def admin_graceful_reset():
        """
        中央サーバの学習状態を学習開始前に戻す。

        処理順:
          1. 現在の current_meta.json をアーカイブして保存
          2. round_updates (メモリ上の集約バッファ) をクリア
          3. SQLite マーカー (received_by_edge) をクリア
          4. current_meta.json を round=1 に初期化
        """
        import time as _time
        reset_ts = _time.strftime("%Y%m%d_%H%M%S")
        logging.info(f"[admin] graceful_reset 要求を受信 ts={reset_ts}")

        prev_round = None
        results = {}

        # 1. 現在の meta を読んでアーカイブ
        try:
            from central_server.endpoints.edge_update import load_meta, save_meta, META_PATH
            meta = load_meta()
            prev_round = int(meta.get("round", 1))
            # アーカイブとして保存
            archive_path = META_PATH.parent / f"meta_archive_{reset_ts}.json"
            archive_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            results["meta_archived"] = str(archive_path)
            logging.info(f"[admin] meta をアーカイブしました: {archive_path}")
        except Exception as e:
            logging.exception("[admin] meta アーカイブに失敗（リセットは続行）")
            results["meta_archived"] = f"failed: {e}"

        # 2. round_updates メモリバッファをクリア
        try:
            from central_server.state import round_updates
            round_updates.clear()
            results["round_updates_cleared"] = True
            logging.info("[admin] round_updates をクリアしました")
        except Exception as e:
            logging.exception("[admin] round_updates クリアに失敗")
            results["round_updates_cleared"] = False

        # 3. SQLite マーカーをクリア
        try:
            from central_server.utils.marker_store import reset_round
            ok = reset_round()
            results["markers_cleared"] = ok
            logging.info(f"[admin] SQLite マーカーをクリアしました ok={ok}")
        except Exception as e:
            logging.exception("[admin] SQLite マーカークリアに失敗")
            results["markers_cleared"] = False

        # 4. current_meta.json を round=1 に初期化
        try:
            from central_server.endpoints.edge_update import load_meta, save_meta
            meta = load_meta()
            meta["round"] = 1
            save_meta(meta)
            results["round_reset"] = True
            logging.info("[admin] current_meta.json を round=1 にリセットしました")
        except Exception as e:
            logging.exception("[admin] current_meta.json のリセットに失敗")
            results["round_reset"] = False

        try:
            central_time_logger.log_event("graceful_reset_complete", {
                "ts": reset_ts,
                "round_before": prev_round,
                "round_after": 1,
                "results": results,
            })
        except Exception:
            pass

        logging.info(f"[admin] graceful_reset 完了 (round {prev_round} → 1)")
        return JSONResponse(content={
            "status": "ok",
            "message": f"中央サーバをリセットしました (round {prev_round} → 1)",
            "round_before": prev_round,
            "round_after": 1,
            "ts": reset_ts,
            "details": results,
        })

    # 端末ID発行API
    class DeviceInfo(BaseModel):
        os_version: Optional[str]
        app_version: Optional[str]

    @app.post("/api/v1/get_terminal_id")
    async def get_terminal_id(info: DeviceInfo):
        terminal_id = str(uuid4())
        # TODO: 端末IDとデバイス情報をDB等に保存
        return {"terminal_id": terminal_id}

    # ラウンド状態取得API
    @app.get("/api/v1/meta")
    async def get_meta():
        # Return the authoritative meta by reading the active current_meta.json
        try:
            # Prefer the endpoint's META loader which knows the actual path used by endpoints
            from central_server.endpoints.edge_update import load_meta
            meta = load_meta()
            return {
                "round": int(meta.get("round", 1)),
                "model_id": meta.get("model_id"),
                "new_model_available": True if meta.get("current_batch_rel") else False,
            }
        except Exception:
            # Fallback to RoundState if edge_update isn't available or load fails
            try:
                round_num = 1
                if hasattr(app.state, 'round_state'):
                    # RoundState currently tracks per-round statuses; keep legacy behaviour
                    # but default to 1 when no explicit current round is available
                    round_num = 1
                return {"round": round_num, "model_id": None, "new_model_available": False}
            except Exception:
                return {"round": 1, "model_id": None, "new_model_available": False}

    # 学習結果アップロードAPI
    class TrainingResult(BaseModel):
        terminal_id: str
        round: int
        accuracy: float
        loss: float
        timestamp: int

    @app.post("/api/v1/upload_training_result")
    async def upload_training_result(result: TrainingResult):
        # TODO: 結果をDB等に保存・処理
        return {"status": "success"}


    from central_server.endpoints.edge_update import META_PATH, save_meta

    # サーバ起動時: 既存 current_meta.json をアーカイブしてからラウンドを1で初期化
    import shutil
    from datetime import datetime
    try:
        # archive central_server/state/current_meta.json (edge_update.META_PATH)
        if META_PATH.exists():
            ts_pre = datetime.now().strftime('%Y%m%d_%H%M%S')
            archive_root = META_PATH.parent / 'archives' / ts_pre
            archive_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(META_PATH, archive_root / 'current_meta.json')
            logging.info(f"起動前アーカイブ: 中央 META を保存しました → {archive_root}")
    except Exception:
        logging.exception("起動時に既存の中央 META をアーカイブできませんでした")

    try:
        # Also archive repository-level state (CONFIG_PATH / META_PATH from central_server.config)
        from central_server.config import STATE_DIR, META_PATH as REPO_META_PATH
        if REPO_META_PATH.exists():
            ts_pre2 = datetime.now().strftime('%Y%m%d_%H%M%S')
            repo_archive = REPO_META_PATH.parent / 'archives' / ts_pre2
            repo_archive.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_META_PATH, repo_archive / 'current_meta.json')
            logging.info(f"起動前アーカイブ: リポジトリ META を保存しました → {repo_archive}")
    except Exception:
        logging.exception("起動時にリポジトリレベルの状態ファイルをアーカイブできませんでした")

    # 起動時: 既存メタのラウンドを引き継いで初期化する。
    # ラウンドを持つメタが存在すればその値を維持し、存在しない場合のみ round=1 から開始する。
    # これによりサーバー再起動でラウンドが巻き戻らない。
    existing_round = 1
    try:
        if META_PATH.exists():
            _existing = json.loads(META_PATH.read_text(encoding='utf-8'))
            existing_round = int(_existing.get('round', 1))
            logging.info(f"起動前アーカイブから round={existing_round} を引き継ぎます")
    except Exception:
        logging.exception("起動時に既存 META のラウンドを読み取れませんでした。round=1 で起動します")

    meta = {
        "round": existing_round,
        "model_id": "demo-mlp-v1",
        "base_hash": "sha256:bootstrap",
        "preferred_dtype": "torch_state_dict",
        "upload_endpoint": "/receive_terminal_weights/{terminal_id}",
        "aggregation_threshold": 1,
        "expected_param_len": 0,
        "order_version": 1,
    }
    try:
        # write to the authoritative META used by endpoints
        save_meta(meta)
        # also overwrite repository-level META (where edge's get_global_model may read)
        try:
            from central_server.config import META_PATH as REPO_META_PATH
            REPO_META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
        except Exception:
            logging.exception("起動時にリポジトリレベル META の書き込みに失敗しました")

        logging.info(f"起動時に current_meta.json を round={existing_round} で初期化しました")
    except Exception:
        logging.exception("起動時に current_meta.json の書き込みに失敗しました")

    # 起動時処理（モデル・設定ファイルの整備など）
    register_startup_events(app)

    # RoundState management
    app.state.round_state = RoundState()

    # AI アドバイザー初期化（API キーが未設定なら自動で無効）
    try:
        from central_server.ai_advisor import init_advisor
        from central_server.config import STATE_DIR
        init_advisor(
            db_path  = STATE_DIR / "training_metrics.db",
            log_dir  = STATE_DIR / "ai_advisor_logs",
        )
    except Exception:
        logging.exception("AI アドバイザーの初期化に失敗しました（サーバー動作には影響しません）")

    @app.on_event("shutdown")
    def shutdown_event():
        app.state.round_state.save_state()
        # current_meta.jsonをタイムスタンプ付きディレクトリにアーカイブ
        from central_server.endpoints.edge_update import META_PATH
        import shutil
        from datetime import datetime
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        archive_dir = META_PATH.parent / ts
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(META_PATH, archive_dir / "current_meta.json")
        logging.info(f"current_meta.json をアーカイブしました → {archive_dir}")
        logging.info("中央サーバを停止しました。ログファイルを閉じました。")

    @app.post("/edge_update")
    async def edge_update_handler(data: dict):
        round_id = data.get("round_id")
        if app.state.round_state.is_round_completed(round_id):
            app.logger.info(f"ラウンド {round_id} は既に完了済みのためデータを無視します。")
            return {"message": "Round already completed, ignoring data."}

        app.state.round_state.mark_round_in_progress(round_id)
        app.logger.info(f"ラウンド {round_id} を処理中です。")

        # ...existing aggregation logic...

        app.state.round_state.mark_round_completed(round_id)
        app.logger.info(f"ラウンド {round_id} を完了としてマークしました。")
        app.logger.info(f"ラウンド {round_id} 状態遷移: 処理中 → 完了")
        return {"message": "Round processed successfully."}

    return app

# run_all.py から "central_server.main:app" で読み込む前提
app = create_app()


if __name__ == "__main__":
    # 対話式ランチャー：想定エッジ台数とポートを選択して起動
    import os
    import time
    import uvicorn
    from central_server.config import CENTRAL_SERVER_HOST, CENTRAL_SERVER_PORT

    def _ask(prompt: str, default: Optional[str] = None) -> str:
        try:
            v = input(f"{prompt}{' ['+default+']' if default else ''}: ").strip()
            return v or (default or "")
        except Exception:
            return default or ""

    print("=== 中央サーバ インタラクティブ起動 ===")
    default_port = os.getenv("CENTRAL_PORT", str(CENTRAL_SERVER_PORT))
    default_edges = os.getenv("EXPECTED_EDGE_COUNT", "1")

    port = _ask("使用するポート番号", default_port)
    edge_count = _ask("想定するエッジサーバ台数 (1/2/3...)", default_edges)

    # 反映（環境変数）
    os.environ["EXPECTED_EDGE_COUNT"] = str(edge_count)
    # 期待エッジ名一覧を自動生成して設定ファイルに書き出し
    try:
        n = int(edge_count)
        expected_ids = [f"edge-server-{i:02d}" for i in range(1, n + 1)]
        payload = {
            "expected_edge_count": n,
            "expected_edge_ids": expected_ids,
            "aggregation_policy": "weighted_mean",
            "required_edges_to_aggregate": n,
            "reaggregation_timeout_sec": 300,
            "auth_required": False,
            "tokens": {},
            "logging_level": "info",
            "model_output_dir": "global_models"
        }
        from pathlib import Path
        cfg_dir = Path(__file__).resolve().parent / "state"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "edges_expected.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 同時に環境変数にも期待IDを設定（必要に応じて参照）
        os.environ["EXPECTED_EDGE_IDS"] = ",".join(expected_ids)
    except Exception:
        pass

    print("\n[設定]")
    print(f"  期待エッジ数 = {edge_count}")
    try:
        print(f"  期待エッジID   = {', '.join(expected_ids)}")
    except Exception:
        pass
    print(f"  中央サーバPORT     = {port}")
    print("\n起動します... (Ctrl+Cで停止)")
    time.sleep(0.3)

    uvicorn.run(app, host=CENTRAL_SERVER_HOST, port=int(port), timeout_keep_alive=120)
