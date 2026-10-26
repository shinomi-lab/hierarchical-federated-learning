# edge_server/endpoints/terminal_update.py
from __future__ import annotations

import asyncio
import logging
import hashlib
import io
import json
import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from collections import defaultdict, deque

import torch
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile, Response, Request, Header
from pydantic import BaseModel

router = APIRouter()
# ========== Marker API ===========
from fastapi.responses import JSONResponse
@router.get("/api/markers/hash/{sha}")
async def marker_check(sha: str):
    # 形式統一
    if not sha.startswith("sha256:"):
        sha = "sha256:" + sha
    exists = sha in _received_hashes
    return JSONResponse(content={"exists": exists})

from aiofiles import os as aio_os, open as aio_open
import httpx

from edge_server.config import EDGE_SERVER_ID, TERMINAL_UPDATE_DIR, RATE_LIMIT_WINDOW
from edge_server.endpoints.aggregation import aggregate_and_send_to_central_server, mark_round_scheduled, clear_round_scheduled, is_round_scheduled
from edge_server.state import UpdateRecord, append_update, bucket_count, state_dict_lock, current_edge_state, save_state, set_processing
from edge_server.utils.file_utils import save_uploaded_file  # 互換のため維持
from edge_server.rate_limiter import RateLimiter
from edge_server.utils.time_logger import edge_time_logger

# 中央メタ同期のためのロック
meta_lock = asyncio.Lock()

# 仮想ルータ遅延マップ (ルータID -> 遅延秒)
VIRTUAL_ROUTER_DELAY_MAP: Dict[str, float] = {
    "RouterA-A": 0.20,  # 200ms
    "RouterB": 0.25,     # 250ms
}


class ClientMeta(BaseModel):
    """クライアントが送ることのできる任意メタ情報（JSON用）。
    `virtual_router_id` を含み、デフォルトは "default_router"。
    """
    virtual_router_id: str = "default_router"

# ラウンドごとに (terminal_id, sha256) を記録して重複を弾く（プロセス再起動で消える）
seen_updates: Dict[int, set[Tuple[str, str]]] = defaultdict(set)

# persisted received hashes for dedupe
RECEIVED_HASHES_FILE = Path(TERMINAL_UPDATE_DIR) / "received_hashes.json"


def _load_received_hashes() -> set:
    try:
        if RECEIVED_HASHES_FILE.exists():
            txt = RECEIVED_HASHES_FILE.read_text(encoding="utf-8")
            data = json.loads(txt)
            if isinstance(data, list):
                return set(data)
    except Exception:
        logging.exception("received_hashes のディスク読み込みに失敗しました")
    return set()


def _save_received_hashes(hashes: set) -> None:
    try:
        tmp = RECEIVED_HASHES_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(sorted(list(hashes)), ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(RECEIVED_HASHES_FILE)
    except Exception:
        logging.exception("received_hashes の原子書き込みに失敗しました。直接書き込みにフォールバックします")
        try:
            RECEIVED_HASHES_FILE.write_text(json.dumps(sorted(list(hashes)), ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            logging.exception("received_hashes のフォールバック直接書き込みに失敗しました")


_received_hashes = _load_received_hashes()
_hash_lock = asyncio.Lock()

# 端末ごとのローカル学習カウンタ（端末が連続で学習を行った回数を追跡）
terminal_local_counters: Dict[str, int] = defaultdict(int)
# 端末ごとの最後に受け取った round を追跡（連続性の判断に利用）
terminal_last_round: Dict[str, int] = {}
# 端末ごとの累積アップロード回数（graceful_reset でリセット、重複は除外）
terminal_upload_counts: Dict[str, int] = defaultdict(int)
# アップロード履歴（タイムスタンプ付き、直近 2000 件、graceful_reset でリセット）
terminal_upload_history: deque = deque(maxlen=2000)
# 端末ごとの staleness 履歴（同期 HFL は常に 0、非同期 HFL は actual staleness 値）
terminal_staleness_accum: Dict[str, List[int]] = defaultdict(list)
# バケットごとに最初の端末が到着した時刻（bucket_fill_latency 計算用）
_bucket_fill_start: Dict[int, float] = {}

# 端末が連続で行うローカル学習回数（デフォルト5）。環境変数で上書き可。
EDGE_LOCAL_TRAIN_STEPS = int(os.getenv("EDGE_LOCAL_TRAIN_STEPS", "5"))

# ========= ユーティリティ =========
def sha256_hex(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()

def _is_valid_ext(filename: str, dtype: str) -> bool:
    fn = (filename or "").lower()
    if dtype == "torch_state_dict":
        return fn.endswith(".pt") or fn.endswith(".pth")
    if dtype == "f32_flat":
        return fn.endswith(".bin") or fn.endswith(".npy")
    if dtype == "meta_bin":
        return fn.endswith(".bin") or fn.endswith(".npy") or fn.endswith(".pt")
    return False

# full-weights 用スペック（端末の flatten 順序と一致させる）
def spec_for_full_mlp(input_size: int, hidden_size: int, output_size: int) -> List[Tuple[str, Tuple[int, ...]]]:
    if hidden_size > 0:
        return [
            ("layer1.weight", (hidden_size, input_size)),
            ("layer1.bias",   (hidden_size,)),
            ("norm1.weight",  (hidden_size,)),   # LayerNorm gamma
            ("norm1.bias",    (hidden_size,)),   # LayerNorm beta
            ("layer2.weight", (hidden_size, hidden_size)),
            ("layer2.bias",   (hidden_size,)),
            ("norm2.weight",  (hidden_size,)),
            ("norm2.bias",    (hidden_size,)),
            ("layer3.weight",    (output_size, hidden_size)),
            ("layer3.bias",      (output_size,)),
        ]
    # H=0 → 線形モデル
    return [("layer3.weight", (output_size, input_size)), ("layer3.bias", (output_size,))]

# bias-only 用スペック（端末の bias-only flatten と完全一致）
def spec_for_bias_only(hidden_size: int, output_size: int) -> List[Tuple[str, Tuple[int, ...]]]:
    if hidden_size > 0:
        return [
            ("layer1.bias", (hidden_size,)),
            ("norm1.bias",  (hidden_size,)),
            ("layer2.bias", (hidden_size,)),
            ("norm2.bias",  (hidden_size,)),
            ("layer3.bias",    (output_size,)),
        ]
    return [("layer3.bias", (output_size,))]

def rebuild_state_dict_from_flat(
    flat: torch.Tensor, spec: List[Tuple[str, Tuple[int, ...]]]
) -> Dict[str, torch.Tensor]:
    """
    flat: 1-D float32 tensor
    spec: [(key, shape), ...] 端末の flatten と同じ順序・サイズ
    """
    sd: Dict[str, torch.Tensor] = {}
    offset = 0
    n = int(flat.numel())
    for key, shape in spec:
        need = 1
        for s in shape:
            need *= s
        if offset + need > n:
            raise ValueError(f"flat too short for {key}: need {need}, have {n - offset}")
        sd[key] = flat[offset : offset + need].reshape(shape).clone()
        offset += need
    if offset != n:
        raise ValueError(f"flat has extra {n - offset} values beyond spec")
    return sd

def _reject_nan_inf(sd: Dict[str, torch.Tensor]) -> None:
    for k, t in sd.items():
        if torch.is_floating_point(t):
            if torch.isnan(t).any() or torch.isinf(t).any():
                raise HTTPException(status_code=422, detail=f"テンソル '{k}' に NaN/Inf が含まれています")


def get_effective_aggregation_threshold() -> int:
    """ローカル単体テスト時に環境変数でしきい値を緩和するユーティリティ。

    - 非同期モード (HFL_MODE=async) の場合は K_EDGE を返す（閾値ベース非同期 FL）。
    - 環境変数 `EDGE_LOCAL_FORCE_SINGLE=1` がセットされていれば常に 1 を返す（ローカル1端末テスト向け）。
    - 環境変数 `EDGE_AGGREGATION_THRESHOLD_OVERRIDE` があればその値を返す（整数、最低1）。
    - それ以外は `current_edge_state.aggregation_threshold` を返す。
    """
    try:
        # 非同期 HFL モードの場合は K_EDGE を優先（環境変数 override は引き続き有効）
        try:
            from edge_server.config import is_async_mode, K_EDGE
            if is_async_mode():
                override = os.getenv("EDGE_AGGREGATION_THRESHOLD_OVERRIDE")
                if override is not None:
                    return max(1, int(override))
                return max(1, int(K_EDGE))
        except Exception:
            pass
        if os.getenv("EDGE_LOCAL_FORCE_SINGLE", "0") in ("1", "true", "True"):
            return 1
        override = os.getenv("EDGE_AGGREGATION_THRESHOLD_OVERRIDE")
        if override is not None:
            v = int(override)
            return max(1, v)
    except Exception:
        # 万が一パースに失敗してもデフォルトを返す
        pass
    return current_edge_state.aggregation_threshold

# ========= 対話的集約フロー =========
async def _ask_user_confirmation(prompt: str) -> bool:
    """
    標準入力経由でユーザーに y/n を問いかける。イベントループをブロックしない。
    stdin が利用できない環境（デーモンなど）では RuntimeError を発生させる。
    """
    if not os.isatty(0):
        # stdin が tty でない場合は対話不可
        try:
            edge_time_logger.log_warn("interactive_not_available", {"reason": "not_tty"})
        except Exception:
            pass
        raise RuntimeError("stdin is not a TTY; interactive confirmation is not available.")

    def _blocking_input() -> str:
        # 改行を挟んでプロンプトを見やすくする
        try:
            edge_time_logger.log_info("interactive_prompt_shown", {"prompt": prompt})
        except Exception:
            pass
        print()
        return input(prompt)

    try:
        # 同期的な input() を別スreadで実行
        res = await asyncio.to_thread(_blocking_input)
        try:
            edge_time_logger.log_info("interactive_prompt_response", {"response": str(res)})
        except Exception:
            pass
        return res.strip().lower() in ("y", "yes")
    except Exception as e:
        try:
            edge_time_logger.log_error("interactive_prompt_error", {"error": repr(e)})
        except Exception:
            pass
        print(f"対話プロンプトでエラーが発生: {e}")
        return False

async def _interactive_aggregation_flow(round_id: int):
    """
    対話形式で集約と送信を行うフロー。
    バックグラウンドタスクとして実行されることを想定。
    """
    try:
        current_count = bucket_count(round_id)
        prompt = (
            f"▶ ラウンド {round_id} の更新が {current_count} 件に達しました。"
            f"集約を開始しますか？ (y/n): "
        )
        if await _ask_user_confirmation(prompt):
            try:
                edge_time_logger.log_info("operator_approved_aggregation", {"round": round_id})
            except Exception:
                pass
            print(f"✅ オペレーターの承認により、ラウンド {round_id} の集約を開始します...")
            # auto_send=True で集約後に即時送信を試みる
            await aggregate_and_send_to_central_server(EDGE_SERVER_ID, round_id, auto_send=True)
        else:
            try:
                edge_time_logger.log_info("operator_declined_aggregation", {"round": round_id})
            except Exception:
                pass
            print(f"❌ オペレーターにより集約は見送られました。更新はラウンド {round_id} に保持されます。")
    except Exception as e:
        # 対話が不可能な環境である場合など
        try:
            edge_time_logger.log_error("interactive_aggregation_error", {"error": repr(e)})
        except Exception:
            pass
        print(f"対話型集約フローでエラーが発生しました: {e}")

# ========= 受け口 =========
@router.post("/receive_terminal_weights/{terminal_id}")
async def receive_terminal_weights(
    terminal_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    # メタ
    round_id: int = Form(..., description="server round id"),
    model_id: str = Form(...),
    base_hash: str = Form(...),
    n_samples: int = Form(...),
    payload_kind: str = Form("full"),
    dtype: str = Form("torch_state_dict"),
    force_aggregate: bool = Form(False),
    # 任意のクライアントメタデータ（JSON文字列など、上限あり）
    client_meta: Optional[str] = Form(None, description="arbitrary client metadata text (UTF-8, <= limit)"),
    # f32_flat のときのみ必須
    input_size: Optional[int] = Form(None),
    hidden_size: Optional[int] = Form(None),
    output_size: Optional[int] = Form(None),
    # optional client-provided checks/ids
    contentSha256: Optional[str] = Form(None),
    reqId: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None),
    virtual_router_id: str = Form("default_router"),
    # 本体
    weights: UploadFile = File(...),
    # optional meta file when sending meta+bin pair
    meta_file: Optional[UploadFile] = File(None),
    # optional session/run metadata from terminal
    run_id: Optional[str] = Form(None, description="terminal run/session id (UUID)"),
    local_seq: Optional[int] = Form(None, description="terminal-local monotonic sequence number"),
    started_at: Optional[str] = Form(None, description="terminal run start timestamp"),
    event_timestamp: Optional[str] = Form(None, description="terminal event timestamp"),
    # 研究メトリクス（アプリ種別・満足度）
    accuracy: Optional[float] = Form(None, description="local training accuracy"),
    loss: Optional[float] = Form(None, description="local training loss"),
    val_accuracy: Optional[float] = Form(None, description="local validation accuracy"),
    val_loss: Optional[float] = Form(None, description="local validation loss"),
    cycle_number: Optional[int] = Form(None, description="which cycle (1-5) in runFiveCycles"),
    epoch_count: Optional[int] = Form(None, description="number of epochs completed"),
    data_samples_count: Optional[int] = Form(None, description="number of training samples used"),
    app_type: Optional[str] = Form(None, description="app type: browser/video/call/other"),
    app_index: Optional[int] = Form(None, description="numeric index of app_type"),
    satisfaction_before: Optional[float] = Form(None, description="terminal satisfaction before AP switch (0.0-1.0)"),
    satisfaction_after: Optional[float] = Form(None, description="terminal satisfaction after AP switch (0.0-1.0)"),
    experiment_group: Optional[str] = Form(None, description="experiment group label (A/B/C/D)"),
    model_version: Optional[str] = Form(None, description="global model version used for training"),
    network_type_at_upload: Optional[str] = Form(None, description="network type at upload time (WIFI/CELLULAR)"),
    battery_at_start: Optional[float] = Form(None, description="battery level at training start (0.0-1.0)"),
    battery_at_end: Optional[float] = Form(None, description="battery level at training end (0.0-1.0)"),
    weight_norm: Optional[float] = Form(None, description="L2 norm of uploaded weights (model drift analysis)"),
    tp_measured_mbps: Optional[float] = Form(None, description="measured throughput in Mbps"),
    rtt_measured_ms: Optional[float] = Form(None, description="measured RTT in ms"),
):
    request_received_at = time.time()
    logger = logging.getLogger("terminal_update")
    logger.info(f"[開始] 重み受信リクエスト: terminal_id={terminal_id}, round_id={round_id}, filename={weights.filename}")
    print(f"\n➡️  端末 '{terminal_id}' からラウンド {round_id} の重み受信リクエストを開始...")

    # X-Run-Id ヘッダからも run_id を取得（フォームフィールドが空の場合のフォールバック）
    if not run_id:
        run_id = request.headers.get("X-Run-Id") or request.headers.get("x-run-id") or None
        if run_id == "none":
            run_id = None

    # --- 仮想ルータ遅延シミュレーション ---
    try:
        delay_time = float(VIRTUAL_ROUTER_DELAY_MAP.get(virtual_router_id, 0.0))
    except Exception:
        delay_time = 0.0
    if delay_time > 0:
        try:
            msg = f"Client {terminal_id} via router '{virtual_router_id}' -> delaying {delay_time} seconds"
            try:
                edge_time_logger.log_info("virtual_router_delay", {"terminal_id": terminal_id, "virtual_router_id": virtual_router_id, "delay_s": delay_time})
            except Exception:
                pass
            print("[virtual_router] ", msg)
            logger.info(msg)
            await asyncio.sleep(delay_time)
        except Exception:
            # best-effort only; do not block processing on logging failures
            pass
    # Structured event logger
    try:
        from edge_server.config import EDGE_SERVER_ID
        from shared.event_logger import get_event_logger
        evt = get_event_logger("edge_terminal_update", edge_id=EDGE_SERVER_ID)
        evt.info("weights_receive_start", {"terminal_id": terminal_id, "round_id": int(round_id), "filename": weights.filename})
    except Exception:
        pass
    # クライアント状態を更新
    try:
        app = request.app
        # クライアント状態更新
        app.client_states[terminal_id] = {
            "last_access": time.strftime("%Y-%m-%d %H:%M:%S"),
            "round": round_id
        }
        # イベント履歴追加
        app.event_log.append({
            "event": "weights_received",
            "terminal_id": terminal_id,
            "round": round_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        # 集約進捗更新
        prog = app.round_progress.setdefault(round_id, {"total": 0, "finished": 0})
        prog["total"] = max(prog["total"], len(app.client_states))
        prog["finished"] += 1
        logger.info(f"[状態] クライアント状態とイベントログを更新しました terminal_id={terminal_id}, round_id={round_id}")
    except Exception as e:
        logger.warning(f"[警告] クライアント状態/イベントログの更新に失敗しました: {e}")
    try:
        edge_time_logger.log_info("weights_receive_start", {"terminal_id": terminal_id, "round_id": round_id, "filename": weights.filename})
        logger.info(f"[ログ] weights_receive_start を記録しました terminal_id={terminal_id}, round_id={round_id}")
    except Exception as e:
        logger.warning(f"[警告] weights_receive_start の記録に失敗しました: {e}")

    # --- 0.4) Allowlist enforcement (static mapping) ---
    try:
        from edge_server.config import is_terminal_allowed, EDGE_SERVER_ID
        if not is_terminal_allowed(terminal_id):
            logger.warning(f"[許可リスト] 端末 '{terminal_id}' は {EDGE_SERVER_ID} では許可されていません")
            raise HTTPException(status_code=403, detail="terminal_not_allowed")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[許可リスト] チェックエラー: {e}")

    # --- 0.5) Authorization (optional) ---
    # If EDGE_UPLOAD_TOKEN is set in env, require Authorization: Bearer <token>
    try:
        from edge_server.config import EDGE_SERVER_ID
        token = os.getenv("EDGE_UPLOAD_TOKEN")
        if token:
            if not authorization or not authorization.startswith("Bearer "):
                logger.warning(f"[認証] Authorization ヘッダがありません terminal_id={terminal_id}")
                raise HTTPException(status_code=401, detail="Authorization ヘッダがありません")
            provided = authorization.split(" ", 1)[1].strip()
            if provided != token:
                logger.warning(f"[認証] トークンが無効です terminal_id={terminal_id}")
                raise HTTPException(status_code=401, detail="トークンが無効です")
            logger.info(f"[認証] 認可成功 terminal_id={terminal_id}")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[認証] トークン検証エラー: {e}")

    # --- 0.6) client_meta サイズ制限の検証（UTF-8バイト長） ---
    try:
        from edge_server.config import MAX_CLIENT_META_BYTES
    except Exception:
        MAX_CLIENT_META_BYTES = 8192
    if client_meta is not None:
        try:
            meta_bytes = client_meta.encode("utf-8")
        except Exception:
            meta_bytes = str(client_meta).encode("utf-8", errors="ignore")
        if len(meta_bytes) > int(MAX_CLIENT_META_BYTES):
            logger.warning(
                f"[メタ] client_meta が大きすぎます: {len(meta_bytes)} バイト (上限 {MAX_CLIENT_META_BYTES}) terminal_id={terminal_id}"
            )
            from shared.error_responses import error_response
            return error_response(413, "client_meta_too_large", "client_meta が許容バイト数を超えています", extra={"limit_bytes": int(MAX_CLIENT_META_BYTES), "received_bytes": len(meta_bytes)}, retry_after=60)

    # --- 0) 非同期モード判定 ---
    try:
        from edge_server.config import is_async_mode, MAX_STALENESS_ROUNDS
        _async_mode = is_async_mode()
        _max_staleness = int(MAX_STALENESS_ROUNDS)
    except Exception:
        _async_mode = False
        _max_staleness = 5

    # --- 0) 次ラウンドのグローバルモデル待機中は重みを受け付けない ---
    # エッジが中央へ集約結果を送信済みで、次ラウンドモデルの配信を待っている間は
    # 端末からの新規重みを受け付けない。端末は 423 を受けたら /api/v1/meta を
    # ポーリングして waiting_for_next_round が false になるまで待機する。
    # 非同期モードでは「全員揃って待つ」概念がないためこのチェックをスキップする。
    if (not _async_mode) and current_edge_state.waiting_for_new_model:
        from shared.error_responses import error_response
        return error_response(
            423,
            "waiting_for_next_round",
            "エッジは中央サーバから次ラウンドのグローバルモデルを待機中です。しばらく後に再試行してください。",
            extra={
                "current_round": int(current_edge_state.round),
                "waiting_for_next_round": True,
            },
            retry_after=10,
        )

    # --- 0) メタ検証（ラウンド不整合の拒否） ---
    # 同期モード: 端末の round_id がサーバの current_round と一致しない場合は拒否する
    # 非同期モード: 過去 MAX_STALENESS_ROUNDS 以内の古い更新は受け入れる（staleness メタとして記録）。
    #   未来の round_id は依然不正として拒否。
    try:
        incoming_r = int(round_id)
        current_r = int(current_edge_state.round)
    except Exception:
        incoming_r = round_id
        current_r = current_edge_state.round

    _staleness = 0
    try:
        _staleness = int(current_r) - int(incoming_r)
    except Exception:
        _staleness = 0

    if _async_mode:
        # 未来の round は拒否
        if incoming_r > current_r:
            logger.warning(
                f"[非同期] 未来 round: 受信={incoming_r}, 現在={current_r} (terminal_id={terminal_id})"
            )
            from shared.error_responses import error_response
            return error_response(409, "round_future", "未来のラウンドからの更新は受け付けません。", extra={"received_round": int(incoming_r), "current_round": int(current_r)}, retry_after=5)
        # staleness 上限超過は破棄
        if _staleness > _max_staleness:
            logger.warning(
                f"[非同期] staleness 超過: staleness={_staleness} > {_max_staleness} (terminal_id={terminal_id})"
            )
            from shared.error_responses import error_response
            return error_response(409, "staleness_exceeded", "更新が古すぎます (staleness 上限超過)。最新モデルを取得してやり直してください。", extra={"staleness": _staleness, "max_staleness": _max_staleness, "received_round": int(incoming_r), "current_round": int(current_r), "next_meta_url": "/api/v1/meta"}, retry_after=5)
        # それ以外は OK（staleness 値は後のログで参照可能にしておく）
        try:
            edge_time_logger.log_info("async_staleness_accepted", {"terminal_id": terminal_id, "received_round": int(incoming_r), "current_round": int(current_r), "staleness": _staleness})
        except Exception:
            pass
    else:
        if incoming_r != current_r:
            logger.warning(
                f"[ラウンド] 不一致: 受信={incoming_r}, 現在={current_r} (terminal_id={terminal_id})"
            )
            # 409 Conflict で明示的にクライアントへ再同期を促す（最新ラウンドを返す）
            from shared.error_responses import error_response
            return error_response(409, "round_mismatch", "ラウンドが一致しません。/api/v1/meta を取得してから再試行してください。", extra={"received_round": int(incoming_r), "current_round": int(current_r), "next_meta_url": "/api/v1/meta"}, retry_after=5)

    # --- 1) メタの基本検証 ---
    if not isinstance(round_id, int) or round_id < 0:
        logger.warning(f"[メタ] round_id が不正です: {round_id} (terminal_id={terminal_id})")
        raise HTTPException(400, detail="round_id が不正です")
    if not model_id or not base_hash:
        logger.warning(f"[メタ] model_id/base_hash がありません (terminal_id={terminal_id})")
        raise HTTPException(400, detail="model_id または base_hash がありません")
    try:
        n_samples = int(n_samples)
    except Exception as e:
        logger.warning(f"[メタ] n_samples は整数である必要があります: {n_samples} (terminal_id={terminal_id}) error={e}")
        raise HTTPException(400, detail="n_samples は整数である必要があります")
    if n_samples <= 0:
        logger.warning(f"[メタ] n_samples は 0 より大きい必要があります: {n_samples} (terminal_id={terminal_id})")
        raise HTTPException(400, detail="n_samples は 0 より大きい必要があります")

    # --- 2) バイト読込 & 署名 ---
    ts = time.strftime("%Y%m%d_%H%M%S")
    save_dir = Path(TERMINAL_UPDATE_DIR) / f"r{round_id}" / terminal_id
    await aio_os.makedirs(save_dir, exist_ok=True)

    logger.info(f"[保存] 重みを {save_dir} へ保存中 (terminal_id={terminal_id}, round_id={round_id})")
    # Stream upload to disk to avoid loading entire payload into memory
    total_bytes = 0
    import hashlib
    hasher = hashlib.sha256()
    save_path = save_dir / f"weights_{ts}.pt" if dtype == "torch_state_dict" else save_dir / f"weights_{ts}.bin"
    try:
        async with aio_open(save_path, mode="wb") as wf:
            while True:
                chunk = await weights.read(1024 * 1024)
                if not chunk:
                    break
                await wf.write(chunk)
                total_bytes += len(chunk)
                hasher.update(chunk)
        logger.info(f"[保存] ファイル保存完了: {save_path} ({total_bytes} バイト, terminal_id={terminal_id}, round_id={round_id})")
    except Exception as e:
        logger.error(f"[エラー] ファイルアップロードエラー: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"アップロードした重みの保存に失敗しました: {e}")

    # 最大受信サイズ（例: 50MB）
    from edge_server.config import MAX_UPLOAD_SIZE
    if total_bytes > MAX_UPLOAD_SIZE:
        logger.warning(f"[ペイロード] サイズが大きすぎます: {total_bytes} バイト (terminal_id={terminal_id}, round_id={round_id})")
        try:
            await aio_os.remove(save_path)
        except Exception:
            pass
        return Response(content="ペイロードが大きすぎます", status_code=413)
    if total_bytes < 4:
        logger.warning(f"[ペイロード] サイズが小さすぎます: {total_bytes} バイト (terminal_id={terminal_id}, round_id={round_id})")
        raise HTTPException(status_code=400, detail="ペイロードが空または小さすぎます")

    content_sig = "sha256:" + hasher.hexdigest()
    logger.info(f"[検証] SHA256 計算結果: {content_sig} (terminal_id={terminal_id}, round_id={round_id})")
    try:
        evt.info("weights_sha_computed", {"terminal_id": terminal_id, "round_id": int(round_id), "sha256": content_sig})
    except Exception:
        pass
    # content_sha256/ContentSha256両方対応
    provided_sha = None
    if contentSha256:
        provided_sha = contentSha256.strip()
    elif hasattr(weights, 'content_sha256'):
        provided_sha = getattr(weights, 'content_sha256', None)
    else:
        # FastAPIのFormで両方受け付けるには追加引数が必要
        try:
            provided_sha = (await Request.form()).get('content_sha256', None)
        except Exception:
            provided_sha = None
    if provided_sha:
        if not provided_sha.startswith("sha256:"):
            provided_sha = "sha256:" + provided_sha
        if provided_sha != content_sig:
            logger.warning(f"[検証] SHA 不一致: 提示={provided_sha}, 計算={content_sig} (terminal_id={terminal_id}, round_id={round_id})")
            try:
                await aio_os.remove(save_path)
                logger.info(f"[クリーンアップ] SHA 不一致のためファイルを削除しました: {save_path}")
            except Exception as e:
                logger.warning(f"[クリーンアップ] SHA 不一致後のファイル削除に失敗しました: {e}")
            raise HTTPException(status_code=400, detail="content_sha256 mismatch")

    # persistent dedupe check
    # If content already persisted in _received_hashes, treat as duplicate, but DO NOT short-circuit.
    # Previously we removed the uploaded file and returned immediately which skipped enqueue/aggregation.
    # Instead, log and continue so the code will create the manifest, enqueue an UpdateRecord (if not
    # already present) and run the aggregation trigger check below.
    async with _hash_lock:
        exists = content_sig in _received_hashes
    if exists:
        logger.warning(f"[重複] 同一重みを再受信しました: sha256={content_sig} (terminal_id={terminal_id}, round_id={round_id})")
        try:
            edge_time_logger.log_warn("duplicate_received", {"terminal_id": terminal_id, "round_id": round_id, "sha256": content_sig})
            logger.info(f"[ログ] duplicate_received を記録しました terminal_id={terminal_id}, round_id={round_id}")
        except Exception as e:
            logger.warning(f"[警告] duplicate_received の記録に失敗しました: {e}")
        # NOTE: do NOT remove `save_path` and do NOT return here. Fall through to manifest/enqueue logic
        # so aggregation criteria will still be checked.
    try:
        edge_time_logger.log_info("weights_bytes_received", {"terminal_id": terminal_id, "round_id": round_id, "bytes": total_bytes, "sha256": content_sig})
        logger.info(f"[ログ] weights_bytes_received を記録しました terminal_id={terminal_id}, round_id={round_id}, bytes={total_bytes}, sha256={content_sig}")
    except Exception as e:
        logger.warning(f"[警告] weights_bytes_received の記録に失敗しました: {e}")
    print(f"  - 受信データ: {total_bytes} バイト, sha256: {content_sig[:16]}...")
    logger.info(f"[完了] 重み受信が完了しました: terminal_id={terminal_id}, round_id={round_id}, bytes={total_bytes}, sha256={content_sig}")

    if weight_norm is None and dtype == "f32_flat":
        try:
            import numpy as np
            arr = np.fromfile(save_path, dtype=np.float32)
            weight_norm = float(np.linalg.norm(arr))
            logger.info(f"[分析] weight_norm を自動計算しました: {weight_norm:.4f}")
        except Exception as e:
            logger.warning(f"weight_norm の自動計算に失敗しました: {e}")

    # --- manifest 保存（デバッグ追跡に有用） ---
    manifest_path = save_dir / f"manifest_{ts}.json"
    manifest = {
        "terminal_id": terminal_id,
        "round": round_id,
        "model_id": model_id,
        "base_hash": base_hash,
        "n_samples": n_samples,
        "payload_kind": payload_kind,
        "dtype": dtype,
        "filename": weights.filename,
        "size": total_bytes,
        "sha256": content_sig,
        "saved_at": ts,
        "reqId": reqId,
    }
    # 受け取った研究メトリクスをすべて manifest に記録（集約時に参照する）
    _opt_fields = {
        "app_type": app_type,
        "app_index": app_index,
        "satisfaction_before": satisfaction_before,
        "satisfaction_after": satisfaction_after,
        "accuracy": accuracy,
        "loss": loss,
        "val_accuracy": val_accuracy,
        "val_loss": val_loss,
        "tp_measured_mbps": tp_measured_mbps,
        "rtt_measured_ms": rtt_measured_ms,
        "battery_at_start": battery_at_start,
        "battery_at_end": battery_at_end,
        "network_type_at_upload": network_type_at_upload,
        "weight_norm": weight_norm,
        "model_version": model_version,
        "cycle_number": cycle_number,
        "epoch_count": epoch_count,
        "experiment_group": experiment_group,
    }
    for k, v in _opt_fields.items():
        if v is not None:
            manifest[k] = v
    if client_meta is not None:
        manifest["client_meta"] = client_meta
        # Android は accuracy/loss を Form フィールドではなく client_meta JSON 内に送ることがある。
        # マニフェストに保存されていなければ client_meta から補完する（集約時の _extract_float_m で参照）。
        try:
            import json as _json_cm
            _cm_d = _json_cm.loads(client_meta) if isinstance(client_meta, str) else {}
            for _cm_key in ("accuracy", "loss", "val_accuracy", "val_loss"):
                if _cm_key not in manifest and _cm_d.get(_cm_key) is not None:
                    manifest[_cm_key] = float(_cm_d[_cm_key])
        except Exception:
            pass
    async with aio_open(manifest_path, mode="w", encoding="utf-8") as manifest_file:
        await manifest_file.write(json.dumps(manifest, ensure_ascii=False, indent=2))
    try:
        evt.info("weights_manifest_saved", {"terminal_id": terminal_id, "round_id": int(round_id), "path": str(manifest_path)})
    except Exception:
        pass
    # --- Telemetry structured logging ---
    try:
        request_duration_ms = int((time.time() - request_received_at) * 1000)
        telemetry = {
            "terminal_id": terminal_id,
            "round_id": int(round_id),
            "n_samples": n_samples,
            "sha256": content_sig,
            "filename": weights.filename,
            "size_bytes": total_bytes,
            "run_id": run_id,
            "local_seq": int(local_seq) if local_seq is not None else None,
            "started_at": started_at,
            "event_timestamp": event_timestamp,
            "client_meta": client_meta,
            "manifest_path": str(manifest_path),
            "saved_path": str(save_path),
            # 研究フィールド（追加）
            "accuracy": accuracy,
            "loss": loss,
            "val_accuracy": val_accuracy,
            "val_loss": val_loss,
            "cycle_number": cycle_number,
            "epoch_count": epoch_count,
            "data_samples_count": data_samples_count,
            "app_type": app_type,
            "app_index": app_index,
            "satisfaction_before": satisfaction_before,
            "satisfaction_after": satisfaction_after,
            "experiment_group": experiment_group,
            "model_version": model_version,
            "network_type_at_upload": network_type_at_upload,
            "battery_at_start": battery_at_start,
            "battery_at_end": battery_at_end,
            "weight_norm": weight_norm,
            "request_duration_ms": request_duration_ms,
        }
        logger.info(f"[テレメトリ] weights_received: {telemetry}")
        try:
            edge_time_logger.log_info("telemetry_received", telemetry)
        except Exception:
            # best-effort: don't fail the request for logging issues
            logger.debug("edge_time_logger.telemetry_received の記録に失敗しました")
    except Exception:
        logger.exception("テレメトリログの出力に失敗しました")

    # persist received hash early to avoid duplicate processing on retries
    try:
        async with _hash_lock:
            _received_hashes.add(content_sig)
            _save_received_hashes(_received_hashes)
        try:
            edge_time_logger.log_info("received_persisted", {"terminal_id": terminal_id, "round_id": round_id, "sha256": content_sig})
        except Exception:
            pass
    except Exception:
        pass

    # --- 3) dtype ごとにパース ---
    filename_ok = _is_valid_ext(weights.filename, dtype)
    if not filename_ok:
        raise HTTPException(status_code=400, detail="重みファイルの拡張子が不正です")

    if dtype == "torch_state_dict":
        # Defer torch.load until aggregation time to avoid holding large state_dicts in memory.
        # We keep the saved file path in UpdateRecord and only load when needed in aggregation.
        state_dict = None
        try:
            edge_time_logger.log_info("weights_saved_deferred", {"terminal_id": terminal_id, "round_id": round_id, "path": str(save_path)})
        except Exception:
            pass
        print("✅ 重みをファイルに保存しました（ロードは集約時まで遅延）。")

    elif dtype == "f32_flat":
        save_path = save_dir / f"weights_{ts}.bin"  # .npyもここにまとめて保存
        # file already written above via streaming loop

        # --- validate expected byte length early using provided dims ---
        if any(v is None for v in (input_size, hidden_size, output_size)):
            # Missing dims will be checked later as well, but fail fast here
            try:
                await aio_os.remove(save_path)
            except Exception:
                pass
            raise HTTPException(status_code=422, detail="f32_flat では input_size, hidden_size, output_size が必須です")

        I = int(input_size)
        H = int(hidden_size)
        O = int(output_size)

        # build spec to compute expected floats
        if payload_kind == "full":
            spec = spec_for_full_mlp(I, H, O)
        elif payload_kind == "bias-only":
            spec = spec_for_bias_only(H, O)
        else:
            try:
                await aio_os.remove(save_path)
            except Exception:
                pass
            raise HTTPException(status_code=400, detail=f"f32_flat の payload_kind が不正です: '{payload_kind}'")

        # compute expected floats
        expected_floats = 0
        for _k, shape in spec:
            need = 1
            for s in shape:
                need *= int(s)
            expected_floats += need
        expected_bytes = expected_floats * 4
        if total_bytes != expected_bytes:
            try:
                await aio_os.remove(save_path)
            except Exception:
                pass
            raise HTTPException(status_code=400, detail=f"重みバイト数が一致しません（期待 {expected_bytes} バイト、実際 {total_bytes} バイト）")

        # Parse f32_flat from file (offload heavy IO to thread)
        flat: Optional[torch.Tensor] = None
        name = (weights.filename or "").lower()
        if name.endswith(".npy"):
            try:
                import numpy as np  # lazy import
            except ModuleNotFoundError:
                raise HTTPException(status_code=422, detail=".npy の解析には NumPy がインストールされている必要があります")
            try:
                arr = await asyncio.to_thread(np.load, str(save_path))
                if arr.dtype != np.float32:
                    arr = arr.astype(np.float32, copy=False)
                flat = torch.from_numpy(arr).reshape(-1)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f".npy の解析に失敗しました: {e}")
        else:
            # .bin を raw little-endian float32 として読み込む using numpy.fromfile
            try:
                import numpy as np
                arr = await asyncio.to_thread(np.fromfile, str(save_path), dtype=np.float32)
                flat = torch.from_numpy(arr).reshape(-1).clone()
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"float32 バッファの解析に失敗しました: {e}")

        if any(v is None for v in (input_size, hidden_size, output_size)):
            raise HTTPException(status_code=422, detail="f32_flat では input_size, hidden_size, output_size が必須です")

        I = int(input_size)   # 入力次元
        H = int(hidden_size)  # 隠れ層次元（線形なら 0）
        O = int(output_size)  # 出力次元

        def get_len_from_spec(s):
            return sum(int(torch.Size(shape).numel()) for _, shape in s)

        try:
            if payload_kind == "full":
                spec = spec_for_full_mlp(I, H, O)
            elif payload_kind == "bias-only":
                spec = spec_for_bias_only(H, O)
            else:
                raise HTTPException(status_code=400, detail=f"f32_flat の payload_kind が不正です: '{payload_kind}'")

            expected_len = get_len_from_spec(spec)
            actual_len = int(flat.numel())
            if actual_len != expected_len:
                raise HTTPException(
                    status_code=422,
                    detail=f"フラット長が不正です actual={actual_len} payload_kind='{payload_kind}' expected={expected_len}"
                )
            state_dict = rebuild_state_dict_from_flat(flat, spec)
            try:
                edge_time_logger.log_info("weights_parsed", {"terminal_id": terminal_id, "round_id": round_id, "dtype": dtype})
            except Exception:
                pass
            print(f"  - パース成功 (f32_flat)。")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"フラット形式から state_dict の再構成に失敗しました: {e}")

    else:
        # Support uploaded meta+bin format: client uploads a `meta_file` (JSON) and `weights` (binary)
        if dtype == "meta_bin":
            # meta_file required and weights must be binary
            if meta_file is None:
                try:
                    await aio_os.remove(save_path)
                except Exception:
                    pass
                raise HTTPException(status_code=422, detail="meta_bin では meta_file が必須です")

            # read meta JSON
            try:
                meta_txt = await meta_file.read()
                meta = json.loads(meta_txt.decode('utf-8'))
            except Exception as e:
                try:
                    await aio_os.remove(save_path)
                except Exception:
                    pass
                raise HTTPException(status_code=422, detail=f"meta_file の解析に失敗しました: {e}")

            # Validate meta.json against schema if available
            try:
                from jsonschema import validate, ValidationError
                schema_path = Path(__file__).resolve().parent.parent.parent / 'docs' / 'meta.schema.json'
                if schema_path.exists():
                    try:
                        with open(schema_path, 'r', encoding='utf-8') as sf:
                            schema = json.load(sf)
                        validate(instance=meta, schema=schema)
                    except ValidationError as ve:
                        try:
                            await aio_os.remove(save_path)
                        except Exception:
                            pass
                        raise HTTPException(status_code=422, detail=f"meta.json のスキーマ検証に失敗しました: {ve.message}")
                    except Exception as e:
                        # If schema file is malformed or validation library errors, surface a 422
                        try:
                            await aio_os.remove(save_path)
                        except Exception:
                            pass
                        raise HTTPException(status_code=422, detail=f"meta.json のスキーマ検証エラー: {e}")
                # else: no schema available -> skip strict validation
            except ModuleNotFoundError:
                # jsonschema not installed; skip validation (best-effort)
                pass

            # validate expected size and sha
            expected_size = meta.get('weights_size')
            expected_sha = meta.get('weights_sha256')
            if expected_size is None or expected_sha is None:
                try:
                    await aio_os.remove(save_path)
                except Exception:
                    pass
                raise HTTPException(status_code=422, detail="meta.json に weights_size または weights_sha256 がありません")

            # size check
            if total_bytes != int(expected_size):
                try:
                    await aio_os.remove(save_path)
                except Exception:
                    pass
                raise HTTPException(status_code=400, detail=f"重みバイト数が一致しません（期待 {expected_size} バイト、実際 {total_bytes} バイト）")

            # checksum already computed as content_sig (sha256:...)
            normalized_expected = expected_sha if expected_sha.startswith('sha256:') else 'sha256:' + expected_sha
            if normalized_expected != content_sig:
                try:
                    await aio_os.remove(save_path)
                except Exception:
                    pass
                raise HTTPException(status_code=400, detail="content_sha256 が meta.json と一致しません")

            # Reconstruct state_dict from weight.bin according to meta['tensors']
            try:
                import numpy as np
            except ModuleNotFoundError:
                raise HTTPException(status_code=422, detail="meta_bin の weight.bin 解析には NumPy が必要です")

            try:
                state_dict = {}
                with open(save_path, 'rb') as f:
                    buf = f.read()
                # ensure little-endian default
                endianness = meta.get('endianness', 'little')
                dtype_meta = meta.get('dtype', 'float32')
                if dtype_meta != 'float32':
                    raise HTTPException(status_code=422, detail=f"meta.json の dtype が未対応です: {dtype_meta}")

                for t in meta.get('tensors', []):
                    name = t['name']
                    offset = int(t['offset'])
                    ln = int(t['length_bytes'])
                    shape = t.get('shape', [])
                    chunk = memoryview(buf)[offset:offset+ln]
                    arr = np.frombuffer(chunk, dtype='<f4')
                    arr = arr.reshape(tuple(shape), order='C')
                    state_dict[name] = torch.from_numpy(arr.copy())
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"meta+bin から state_dict の再構成に失敗しました: {e}")

        else:
            raise HTTPException(415, detail=f"未対応の dtype です: {dtype}")

    # NaN/Inf 検査: state_dict が既にロードされている場合のみ実行。
    # (torch_state_dict は遅延ロードしているためここでは検査をスキップします)
    try:
        if state_dict is not None:
            _reject_nan_inf(state_dict)
    except Exception:
        raise

    # --- 4) 共有キューに push & しきい値で集約トリガ ---
    async with state_dict_lock:
        try:
            edge_time_logger.log_info("enqueue_start", {"terminal_id": terminal_id, "round_id": round_id})
        except Exception:
            pass
        print(f"  - ロック取得。キューに登録処理を開始...")
        # 二重POST対策の最終チェック
        if (terminal_id, content_sig) in seen_updates[round_id]:
            try:
                edge_time_logger.log_warn("duplicate_update_ignored", {"terminal_id": terminal_id, "round_id": round_id, "sha256": content_sig})
            except Exception:
                pass
            print(f"  - ⚠️ 重複リクエストを検出、無視します。")

            # --- 重要 ---
            # 重複受信であっても、既に条件を満たしていれば集約をトリガーする。
            # 以前はここで即リターンしていたため集約判定がスキップされ、永遠に先へ進まない状況が発生していた。
            try:
                # 現在のバケット件数と閾値を確認
                # 非同期モード: バケットは current_edge_state.round に入っているため同じ round で確認する
                _dup_trigger_round = int(current_edge_state.round) if _async_mode else int(round_id)
                current = bucket_count(_dup_trigger_round)
                effective_threshold = get_effective_aggregation_threshold()
                should_aggregate = (current >= effective_threshold)
                if should_aggregate:
                    try:
                        already = await is_round_scheduled(_dup_trigger_round)
                        if not already:
                            marked = await mark_round_scheduled(_dup_trigger_round)
                            if marked:
                                # Try to schedule immediately on the event loop; if that fails
                                # (should be rare inside FastAPI), add to BackgroundTasks as fallback
                                scheduled_method = None
                                try:
                                    asyncio.create_task(aggregate_and_send_to_central_server(EDGE_SERVER_ID, _dup_trigger_round, auto_send=True))
                                    scheduled_method = "immediate"
                                except Exception:
                                    try:
                                        background_tasks.add_task(asyncio.create_task, aggregate_and_send_to_central_server(EDGE_SERVER_ID, _dup_trigger_round, auto_send=True))
                                        scheduled_method = "background"
                                    except Exception:
                                        logger.exception("集約のスケジュールに失敗しました（即時・バックグラウンド両方）")
                                # explicit debug logging about scheduling outcome
                                try:
                                    edge_time_logger.log_info("aggregation_scheduling_result", {
                                        "round_id": _dup_trigger_round,
                                        "original_round_id": round_id,
                                        "method": scheduled_method,
                                        "trigger": "duplicate",
                                        "terminal_id": terminal_id,
                                        "async": bool(_async_mode),
                                    })
                                except Exception:
                                    pass
                                logger.info(f"[Edge] 重複によりラウンド {_dup_trigger_round} の集約をスケジュールしました (terminal={terminal_id}), method={scheduled_method}")
                    except Exception:
                        logger.exception("重複パスでの集約スケジュールに失敗しました")
            except Exception:
                logger.exception("重複パスでの集約条件評価に失敗しました")

            from datetime import datetime
            ack_ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            return JSONResponse(status_code=200, content={"ack": True, "status": "duplicate_ignored", "sha256": content_sig, "ack_timestamp": ack_ts})

        # この更新を記録
        seen_updates[round_id].add((terminal_id, content_sig))
        # 重複でないことが確定してから staleness を蓄積（重複リクエストで水増しされないよう）
        terminal_staleness_accum[terminal_id].append(_staleness)

        try:
            edge_time_logger.log_info("update_enqueued", {"terminal_id": terminal_id, "round_id": round_id, "sha256": content_sig})
        except Exception:
            pass
        print(f"  - 新しい更新レコードをキューに追加します。")
        new_record = UpdateRecord(
            terminal_id=terminal_id,
            state_dict=state_dict,
            n_samples=n_samples,
            sig=content_sig,
            path=str(save_path.resolve()),
            manifest=str(manifest_path.resolve()),
            run_id=run_id,
            local_seq=int(local_seq) if local_seq is not None else None,
            started_at=started_at,
            event_ts=event_timestamp,
            client_meta=client_meta,
            staleness=int(_staleness),
        )
        # 非同期モード時: 古い round_id で送られた更新も「現在 round」のバケットに入れる。
        # これにより、staleness 受信が現在 round の集約閾値に寄与する。
        # 元の round_id は staleness ログ (async_staleness_accepted) に記録済みのためメタとして保持されている。
        try:
            _bucket_round = int(current_edge_state.round) if _async_mode else int(round_id)
        except Exception:
            _bucket_round = round_id
        append_update(_bucket_round, new_record)
        # 累積アップロードカウンタ + タイムスタンプ履歴を更新
        try:
            terminal_upload_counts[terminal_id] = terminal_upload_counts.get(terminal_id, 0) + 1
            _now_ts = time.time()
            # バケットへの最初の到着時刻を記録（bucket_fill_latency 計算用）
            if _bucket_round not in _bucket_fill_start:
                _bucket_fill_start[_bucket_round] = _now_ts
            terminal_upload_history.append({
                "terminal_id": terminal_id,
                "ts": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
                "round_id": int(round_id),
                "bucket_round": int(_bucket_round),
                "n_samples": int(n_samples),
                "upload_no": terminal_upload_counts[terminal_id],
                "staleness": int(_staleness),
                "hfl_mode": "async" if _async_mode else "sync",
                "tp_measured_mbps": tp_measured_mbps,
                "rtt_measured_ms": rtt_measured_ms,
                "val_accuracy": val_accuracy,
                "val_loss": val_loss,
                "battery_at_start": battery_at_start,
                "battery_at_end": battery_at_end,
                "network_type_at_upload": network_type_at_upload,
            })
        except Exception:
            pass
        # --- 診断ログ: append直後のバケット状態を記録（根本原因調査用） ---
        try:
            from edge_server.state import terminal_state_dicts as _tsd_ref
            edge_time_logger.log_event("bucket_after_append", {
                "terminal_id": terminal_id,
                "bucket_round": int(_bucket_round),
                "bucket_size_after": len(_tsd_ref.get(_bucket_round, [])),
                "id_terminal_state_dicts": id(_tsd_ref),
            })
        except Exception:
            pass
        if _async_mode and _bucket_round != round_id:
            try:
                edge_time_logger.log_info("async_bucket_redirected", {
                    "terminal_id": terminal_id,
                    "original_round_id": int(round_id),
                    "bucket_round": int(_bucket_round),
                    "staleness": int(_staleness),
                })
            except Exception:
                pass
        # record ledger for this terminal so edge can reason about continuity
        try:
            from edge_server.state import record_terminal_ledger
            record_terminal_ledger(terminal_id, run_id, int(local_seq) if local_seq is not None else None)
        except Exception:
            logging.exception("端末レジャーの記録に失敗しました")

        # --- Immediate aggregation scheduling: ensure single-client (or threshold-satisfied)
        # 非同期モード: append_update で _bucket_round に入れたので、カウントも _bucket_round で確認する
        try:
            # Evaluate current bucket and effective threshold
            try:
                current_now = bucket_count(_bucket_round)
                effective_threshold_now = get_effective_aggregation_threshold()
            except Exception:
                current_now = None
                effective_threshold_now = None
            should_agg_now = False
            try:
                if current_now is not None and effective_threshold_now is not None:
                    should_agg_now = (current_now >= effective_threshold_now) or bool(force_aggregate)
            except Exception:
                should_agg_now = False

            if should_agg_now:
                try:
                    already = await is_round_scheduled(_bucket_round)
                    if not already:
                        marked = await mark_round_scheduled(_bucket_round)
                        if marked:
                            # schedule with explicit outcome tracking
                            scheduled_method = None
                            try:
                                asyncio.create_task(aggregate_and_send_to_central_server(EDGE_SERVER_ID, _bucket_round, auto_send=True))
                                scheduled_method = "immediate"
                            except Exception:
                                try:
                                    background_tasks.add_task(asyncio.create_task, aggregate_and_send_to_central_server(EDGE_SERVER_ID, _bucket_round, auto_send=True))
                                    scheduled_method = "background"
                                except Exception:
                                    logger.exception("即時集約のスケジュールに失敗しました（即時・バックグラウンド両方）")
                            try:
                                edge_time_logger.log_info("aggregation_scheduling_result", {
                                    "round_id": _bucket_round,
                                    "original_round_id": round_id,
                                    "method": scheduled_method,
                                    "trigger": "post_enqueue",
                                    "current": current_now,
                                    "threshold": effective_threshold_now,
                                    "async": bool(_async_mode),
                                })
                            except Exception:
                                pass
                            logger.info(f"[Edge] 即時集約のスケジュール試行 round {_bucket_round} (現在={current_now}, 閾値={effective_threshold_now}), method={scheduled_method}")
                except Exception:
                    logger.exception("即時集約のスケジュールに失敗しました（エンキュー後）")
        except Exception:
            logger.exception("即時集約条件の評価に失敗しました（エンキュー後）")

    # --- 4.5) 端末ローカル学習カウンタ更新 ---
    try:
        last = terminal_last_round.get(terminal_id)
        # ラウンド番号が 1 のときは新セッション開始とみなす
        if int(round_id) == 1 or last is None:
            terminal_local_counters[terminal_id] = 1
        else:
            # 連続増加している場合のみカウントを増やす
            try:
                if int(round_id) > int(last):
                    terminal_local_counters[terminal_id] = terminal_local_counters.get(terminal_id, 0) + 1
            except Exception:
                terminal_local_counters[terminal_id] = terminal_local_counters.get(terminal_id, 0) + 1
        terminal_last_round[terminal_id] = int(round_id)
    except Exception:
        # 失敗しても本体フローは止めない
        pass

    # ローカル学習が規定回数に達したら、エッジ側で集約→中央送信→グローバルラウンド更新を行う
    if terminal_local_counters.get(terminal_id, 0) >= EDGE_LOCAL_TRAIN_STEPS:
        try:
            edge_time_logger.log_info("local_training_cycle_completed", {"terminal_id": terminal_id, "round_id": round_id, "steps": EDGE_LOCAL_TRAIN_STEPS})
        except Exception:
            pass
        print(
            f"🔔 端末 {terminal_id} がローカル学習を {EDGE_LOCAL_TRAIN_STEPS} 回完了 — ラウンド {current_edge_state.round} のエッジ集約を起動します"
        )

        async def _handle_local_cycle_completion(tid: str, incoming_round: int):
            try:
                # Dedup: skip if this round is already scheduled via the main aggregation path
                already = await is_round_scheduled(incoming_round)
                if already:
                    logger.info(f"[local_cycle] ラウンド {incoming_round} は既にスケジュール済み — スキップします")
                    return
                # バケット閾値チェック: 全端末が揃っていない場合は集約しない（同期学習の保証）
                # 閾値に満たない場合はマークせずに戻る。他の端末が送信した際の閾値パスで集約が発火する。
                current_count = bucket_count(incoming_round)
                threshold = get_effective_aggregation_threshold()
                if current_count < threshold:
                    logger.info(
                        f"[local_cycle] バケット未達 ({current_count}/{threshold}) ラウンド {incoming_round} "
                        f"— 端末 {tid} のローカルサイクル完了だが他端末の送信を待ちます"
                    )
                    return
                marked = await mark_round_scheduled(incoming_round)
                if not marked:
                    logger.info(f"[local_cycle] ラウンド {incoming_round} のマークに失敗（既にマーク済み）— スキップします")
                    return
                # mark processing start and persist
                try:
                    set_processing(True)
                    save_state(in_progress=True)
                except Exception:
                    pass
                # Use incoming_round (captured at schedule time) — not current_edge_state.round,
                # which may have been advanced by the background poller between schedule and execution.
                await aggregate_and_send_to_central_server(EDGE_SERVER_ID, incoming_round, auto_send=True)
                # Round advancement is handled solely by the background poller after the central
                # confirms a new global model. Do NOT bump current_edge_state.round here.
                try:
                    save_state(in_progress=False)
                except Exception:
                    logging.exception("ローカル学習サイクル完了後の状態保存に失敗しました")
            except Exception:
                logging.exception("ローカル学習サイクル完了の処理に失敗しました")
            finally:
                # reset the per-terminal local counter so subsequent cycles start fresh
                try:
                    terminal_local_counters[tid] = 0
                except Exception:
                    pass

        # schedule background task (non-blocking) - ensure coroutine is scheduled via asyncio.create_task
        try:
            try:
                asyncio.create_task(_handle_local_cycle_completion(terminal_id, round_id))
            except Exception:
                background_tasks.add_task(asyncio.create_task, _handle_local_cycle_completion(terminal_id, round_id))
        except Exception:
            logging.exception("ローカル学習サイクル完了後の集約タスクのスケジュールに失敗しました")

    # 集約を開始するかどうかを判定
    # 非同期モード: append_update で _bucket_round (=current_edge_state.round) に入れているので、
    # 集約判定もそのバケットでカウントする。
    try:
        _trigger_round = int(_bucket_round) if _async_mode else int(round_id)
    except Exception:
        _trigger_round = round_id
    current = bucket_count(_trigger_round)
    effective_threshold = get_effective_aggregation_threshold()
    needed = max(0, effective_threshold - current)
    should_aggregate = (current >= effective_threshold) or force_aggregate

    # デバッグログを追加して現在の件数を確認
    try:
        edge_time_logger.log_info("bucket_status", {"round_id": round_id, "trigger_round": _trigger_round, "current": current, "threshold": effective_threshold, "async": bool(_async_mode)})
    except Exception:
        pass
    print(f"💡 [ラウンド {round_id}] 更新を受け付けました。現在の件数: {current}/{effective_threshold}")

    # --- ロックを解放してから、バックグラウンドタスクを起動 ---
    # 非同期モード: 集約スケジュールは _trigger_round (=current_edge_state.round) に対して行う。
    # 同期モード: 従来通り incoming round_id に対して。
    _sched_round = _trigger_round
    if should_aggregate:
        try:
            edge_time_logger.log_info("aggregation_triggered_console", {"round_id": _sched_round, "original_round_id": round_id})
        except Exception:
            pass
        print(f"🚀 ラウンド {_sched_round} の集約を開始します...")
        edge_time_logger.log_event("aggregation_triggered", {
            "round_id": _sched_round,
            "original_round_id": round_id,
            "current_count": current,
            "threshold": effective_threshold,
            "async": bool(_async_mode),
        })

        # Prevent scheduling the same round multiple times due to rapid duplicate terminal submissions.
        try:
            # mark as scheduled; if already scheduled, skip
            already = await is_round_scheduled(_sched_round)
            if already:
                try:
                    edge_time_logger.log_warn("aggregation_already_scheduled", {"round_id": _sched_round})
                except Exception:
                    pass
                print(f"⚠️ ラウンド {_sched_round} の集約は既にスケジュール済みです。重複スケジュールをスキップします。")
            else:
                marked = await mark_round_scheduled(_sched_round)
                if not marked:
                    try:
                        edge_time_logger.log_warn("aggregation_mark_failed", {"round_id": _sched_round})
                    except Exception:
                        pass
                    print(f"⚠️ ラウンド {_sched_round} のスケジュール済みマークに失敗しました（既にマーク済み）。スケジュールをスキップします。")
                else:
                        # Attempt scheduling and record outcome; if both fail, clear the scheduled marker.
                        scheduled_method = None
                        try:
                            try:
                                asyncio.create_task(aggregate_and_send_to_central_server(EDGE_SERVER_ID, _sched_round, auto_send=True))
                                scheduled_method = "immediate"
                            except Exception:
                                background_tasks.add_task(asyncio.create_task, aggregate_and_send_to_central_server(EDGE_SERVER_ID, _sched_round, auto_send=True))
                                scheduled_method = "background"
                        except Exception:
                            # scheduling failed: clear marker so future attempts can schedule
                            await clear_round_scheduled(_sched_round)
                            logging.exception("集約バックグラウンドタスクのスケジュールに失敗しました。スケジュール済みマーカーを解除しました")
                        try:
                            edge_time_logger.log_info("aggregation_scheduling_result", {
                                "round_id": _sched_round,
                                "original_round_id": round_id,
                                "method": scheduled_method,
                                "trigger": "threshold_reached",
                            })
                        except Exception:
                            pass
                        logger.info(f"[Edge] 集約スケジュール試行 round {_sched_round} (閾値パス), method={scheduled_method}")
        except Exception:
            logging.exception("スケジュール済みラウンドガードの適用に失敗しました。とりあえずスケジュールを試行します")
            try:
                asyncio.create_task(aggregate_and_send_to_central_server(EDGE_SERVER_ID, _sched_round, auto_send=True))
            except Exception:
                logging.exception("ガード失敗後の集約スケジュールにも失敗しました")

    from datetime import datetime
    ack_ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    try:
        edge_time_logger.log_info("weights_received", {
        "terminal_id": terminal_id,
        "round_id": round_id,
        "model_id": model_id,
        "n_samples": n_samples,
        "ack_timestamp": ack_ts,
    })
    except Exception:
        pass
    print(f"✅ 端末 '{terminal_id}' へ応答します。応答種別=weights_received ack={ack_ts}")

    # 研究メトリクスを中央サーバへ転送（バックグラウンド・ノンブロッキング）
    # client_meta JSON からフォールバック値を取得
    _metric = metric_from_client_meta(client_meta, terminal_id)
    if _metric is not None:
        # 新端末: 端末が採番した data_id のまま全項目を転送（再送は端末の未送信キューが担う）
        # edge_round_id: エッジ集約のラウンド（async では端末送信ラウンドと乖離するため併記, async-hfl 由来）
        _metric.setdefault("edge_round_id", int(current_edge_state.round))
        background_tasks.add_task(forward_metric_to_central, _metric)
    else:
        # 旧端末: 従来の転送
        _fwd_tp = tp_measured_mbps
        _fwd_rtt = rtt_measured_ms
        _fwd_app_type = app_type
        _fwd_sat_before = satisfaction_before
        _fwd_sat_after = satisfaction_after
        _fwd_accuracy = accuracy
        _fwd_loss = loss
        if client_meta:
            try:
                import json as _json
                _cm = _json.loads(client_meta)
                if _fwd_tp is None and _cm.get("tp_measured_mbps") is not None:
                    _fwd_tp = float(_cm["tp_measured_mbps"])
                if _fwd_rtt is None and _cm.get("rtt_measured_ms") is not None:
                    _fwd_rtt = float(_cm["rtt_measured_ms"])
                if _fwd_app_type is None and _cm.get("app_type"):
                    _fwd_app_type = str(_cm["app_type"])
                if _fwd_sat_before is None and _cm.get("satisfaction_before") is not None:
                    _fwd_sat_before = float(_cm["satisfaction_before"])
                if _fwd_sat_after is None and _cm.get("satisfaction_after") is not None:
                    _fwd_sat_after = float(_cm["satisfaction_after"])
                if _fwd_accuracy is None and _cm.get("accuracy") is not None:
                    _fwd_accuracy = float(_cm["accuracy"])
                if _fwd_loss is None and _cm.get("loss") is not None:
                    _fwd_loss = float(_cm["loss"])
            except Exception:
                pass
        try:
            from edge_server.config import EDGE_SERVER_ID
            from edge_server.main import send_training_metrics_to_central_server
            background_tasks.add_task(
                send_training_metrics_to_central_server,
                edge_id=EDGE_SERVER_ID,
                round=round_id,
                accuracy=_fwd_accuracy,
                loss=_fwd_loss,
                app_type=_fwd_app_type,
                app_index=app_index,
                satisfaction_before=_fwd_sat_before,
                satisfaction_after=_fwd_sat_after,
                terminal_id=terminal_id,
                tp_measured_mbps=_fwd_tp,
                rtt_measured_ms=_fwd_rtt,
                edge_round_id=int(current_edge_state.round),
            )
        except Exception:
            pass  # メトリクス転送失敗でも重み受信の ACK はそのまま返す

    # Return an explicit ACK object so clients have a clear success signal
    resp_content = {
        "status": "weights_received",
        "ack": True,
        "ack_timestamp": ack_ts,
        "terminal_id": terminal_id,
        "sha256": content_sig,
        "n_samples": n_samples,
        "round_id": round_id,
        "bucket_now": current,
        "needed_to_aggregate": needed,
        "forced": bool(force_aggregate),
    }
    return JSONResponse(status_code=200, content=resp_content)

# レートリミッターのインスタンスを作成
# Use configured RATE_LIMIT_WINDOW; if <= 0 then disable limiting
meta_rate_limiter = RateLimiter(window_size=float(RATE_LIMIT_WINDOW) if RATE_LIMIT_WINDOW is not None else 2.0)

# ========= 端末が参照するメタ =========
@router.get("/api/v1/meta")
async def get_meta(request: Request):
    """
    端末が学習開始前に取得するメタ情報。
    中央サーバの /get_global_model からもらう設計なら、ここで同期しても良い。
    """
    # クライアントのIPアドレスを取得してレート制限を適用
    client_ip = request.client.host if request.client else "unknown"
    # Allow disabling rate limits in development by env var
    disable_rl = os.getenv("EDGE_DISABLE_RATE_LIMITS", "0").lower() in ("1", "true", "yes")
    if disable_rl or getattr(meta_rate_limiter, 'window_size', 0) <= 0:
        should_limit = False
        wait_time = 0.0
    else:
        should_limit, wait_time = meta_rate_limiter.should_limit(client_ip)

    if should_limit:
        # 429 Too Many Requests
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "message": "Too many requests, please wait",
                "wait_seconds": wait_time
            },
            headers={"Retry-After": str(int(wait_time))}
        )

    effective_threshold = get_effective_aggregation_threshold()

    meta_data = {
        "edge_id": EDGE_SERVER_ID,
        "round": current_edge_state.round,
        "model_id": current_edge_state.model_id,
        "base_hash": current_edge_state.base_hash,
        "preferred_dtype": "torch_state_dict",   # f32_flat を使うなら "f32_flat"
        "upload_endpoint": "/receive_terminal_weights/{terminal_id}",
        "aggregation_threshold": effective_threshold,
        "expected_param_len": 0,                  # f32_flat 厳密運用時は総要素数を入れても良い
        "order_version": 1,
        "waiting_for_next_round": current_edge_state.waiting_for_new_model,
    }

    # SP-B / SP3d: 要求元端末について解決したベースラインモードとサイクル数（端末上書き > 全体既定。無ければ項目を出さない）
    try:
        await _ensure_experiment_control_fresh()
    except Exception:
        pass
    _tid = request.headers.get("X-Terminal-Id")
    meta_data.update(resolve_control_for_terminal(EXPERIMENT_CONTROL, _tid))
    meta_data["experiment_control_version"] = EXPERIMENT_CONTROL.get("version", 0)

    # SP3b R1: このエッジの HFL モード（起動時の HFL_MODE）。端末はアプリ設定よりこちらを優先する
    try:
        from edge_server.config import is_async_mode
        meta_data["hfl_mode"] = "async" if is_async_mode() else "sync"
    except Exception:
        pass

    # レート制限情報をヘッダーに追加
    response = JSONResponse(content=meta_data)
    response.headers["X-RateLimit-Limit"] = "1"  # 1リクエスト/2秒
    response.headers["X-RateLimit-Reset"] = str(int(time.time() + wait_time))
    
    return response

async def forward_json_to_central(path: str, body: dict, transport: Optional[httpx.AsyncBaseTransport] = None) -> Tuple[int, str]:
    """本文をそのまま中央の path へ JSON 転送する。接続できなければ (0, 理由)。"""
    from edge_server.config import CENTRAL_SERVER_URL
    url = f"{CENTRAL_SERVER_URL.rstrip('/')}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as c:
            resp = await c.post(url, json=body)
            return resp.status_code, resp.text
    except Exception as e:
        logging.getLogger("edge.metrics").warning("forward to central %s failed: %s", path, e)
        return 0, str(e)


async def forward_metric_to_central(body: dict, transport: Optional[httpx.AsyncBaseTransport] = None) -> Tuple[int, str]:
    """メトリクス本文をそのまま中央へ JSON 転送する（edge_id を付け足す）。接続できなければ (0, 理由)。"""
    payload = dict(body)
    payload.setdefault("edge_id", EDGE_SERVER_ID)
    return await forward_json_to_central("upload_training_metrics", payload, transport=transport)


# ===== 実験制御（SP-B）: 中央の全体既定＋端末上書きを保持し /meta で端末別に配る =====
EXPERIMENT_CONTROL: dict = {"baseline_mode": None, "overrides": {}, "num_cycles": None, "num_cycles_overrides": {},
                            "version": 0}


def _valid_num_cycles(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 100


def resolve_control_for_terminal(control: dict, terminal_id: Optional[str]) -> dict:
    """端末上書き > 全体既定で解決した {"baseline_mode", "num_cycles"}。値が無い（不正な）キーは含めない。"""
    out: dict = {}
    ov = control.get("overrides") or {}
    mode = ov.get(terminal_id) if terminal_id in ov else control.get("baseline_mode")
    if mode:
        out["baseline_mode"] = mode
    nov = control.get("num_cycles_overrides") or {}
    n = nov.get(terminal_id) if terminal_id in nov else control.get("num_cycles")
    if _valid_num_cycles(n):
        out["num_cycles"] = n
    return out


def refresh_experiment_control(fetch=None) -> None:
    """中央の実験制御を取得して保持する。失敗時は直前の値を維持する。"""
    global EXPERIMENT_CONTROL
    from edge_server.config import CENTRAL_SERVER_URL
    from edge_server.startup import fetch_json
    f = fetch or fetch_json
    try:
        c = f(f"{CENTRAL_SERVER_URL.rstrip('/')}/api/v1/experiment_control", 5)
        if isinstance(c, dict):
            EXPERIMENT_CONTROL = {"baseline_mode": c.get("baseline_mode"), "overrides": c.get("overrides") or {},
                                  "num_cycles": c.get("num_cycles"),
                                  "num_cycles_overrides": c.get("num_cycles_overrides") or {},
                                  "version": c.get("version", 0)}
    except Exception as e:
        logging.getLogger("edge.control").debug("experiment_control refresh failed: %s", e)


# エッジの中央ポーラー（background_tasks）は起動されていないため、/meta 要求時に TTL 切れなら取り直す
_CONTROL_TTL_S = float(os.getenv("EXPERIMENT_CONTROL_TTL_S", "5"))
_control_fetched_at = 0.0


async def _ensure_experiment_control_fresh() -> None:
    global _control_fetched_at
    if time.time() - _control_fetched_at < _CONTROL_TTL_S:
        return
    _control_fetched_at = time.time()
    await asyncio.to_thread(refresh_experiment_control)


@router.post("/api/v1/experiment_events")
async def forward_experiment_event(request: Request):
    """端末の実験イベント（モード切替など）を中央へ透過転送する。中央が保存したときだけ 200。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    code, text = await forward_json_to_central("api/v1/experiment_events", body)
    if 200 <= code < 300:
        return JSONResponse({"status": "ok"})
    if 400 <= code < 500:
        return JSONResponse({"error": "central_rejected", "message": text[:500]}, status_code=code)
    return JSONResponse({"error": "central_unavailable", "message": text[:500]}, status_code=502)


def metric_from_client_meta(client_meta: Optional[str], terminal_id: str) -> Optional[dict]:
    """重みアップロードの client_meta に同梱された metric（data_id 付き）を取り出す。"""
    if not client_meta:
        return None
    try:
        cm = json.loads(client_meta)
    except Exception:
        return None
    m = cm.get("metric") if isinstance(cm, dict) else None
    if not isinstance(m, dict) or not m.get("data_id"):
        return None
    out = dict(m)
    out.setdefault("terminal_id", terminal_id)
    return out


@router.post("/upload_training_metrics")
async def upload_training_metrics(request: Request):
    """端末のメトリクス本文をそのまま中央へ転送し、中央が保存できたときだけ 200 を返す。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "message": "リクエスト本文を JSON として解析できませんでした"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_json", "message": "JSON オブジェクトが必要です"}, status_code=400)
    code, text = await forward_metric_to_central(body)
    if 200 <= code < 300:
        return JSONResponse({"status": "ok", "data_id": body.get("data_id")})
    if 400 <= code < 500:
        return JSONResponse({"error": "central_rejected", "message": text[:500]}, status_code=code)
    return JSONResponse({"error": "central_unavailable", "message": text[:500]}, status_code=502)


@router.get("/api/v1/openapi.json")
async def get_openapi_schema():
    """
    Serve the OpenAPI JSON schema to clients.
    """
    from fastapi.openapi.utils import get_openapi
    from edge_server.main import app  # Import the FastAPI app instance

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
    )
    return JSONResponse(content=openapi_schema)
