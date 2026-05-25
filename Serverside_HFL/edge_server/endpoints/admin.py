"""edge_server/endpoints/admin.py
管理用エンドポイント。

POST /admin/graceful_reset
    - 現在の状態をディスクに保存
    - ラウンド番号・集約バケット・ローカルカウンタをすべて初期化
    - 学習開始前の状態に戻す
"""
from __future__ import annotations

import logging
import time

from typing import Any, Dict, List

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/admin", tags=["admin"])
log = logging.getLogger("edge.admin")


@router.get("/topology_snapshot")
async def topology_snapshot():
    """
    運用向けスナップショット: このエッジの edge_id・許可リスト・端末レジャー・メモリ上の更新一覧。
    中央サーバの /ops が各エッジに HTTP で問い合わせる用途。
    """
    from edge_server.config import EDGE_SERVER_ID, TERMINAL_ALLOWLIST
    from edge_server.state import current_edge_state, terminal_ledgers, terminal_state_dicts

    allow = TERMINAL_ALLOWLIST.get(EDGE_SERVER_ID, []) or []
    allow_mode = "all" if not allow else "restricted"

    updates_rows: List[Dict[str, Any]] = []
    for rid, records in sorted(terminal_state_dicts.items(), key=lambda x: x[0]):
        for rec in records:
            sig = rec.sig or ""
            if len(sig) > 48:
                sig = sig[:48] + "…"
            updates_rows.append(
                {
                    "round_id": rid,
                    "terminal_id": rec.terminal_id,
                    "n_samples": rec.n_samples,
                    "sig": sig,
                    "run_id": rec.run_id,
                }
            )

    ledgers_out: Dict[str, Any] = {tid: dict(v) for tid, v in terminal_ledgers.items()}

    return JSONResponse(
        {
            "edge_id": EDGE_SERVER_ID,
            "allow_mode": allow_mode,
            "allowlist_terminal_ids": list(allow),
            "aggregation_threshold": current_edge_state.aggregation_threshold,
            "current_round": current_edge_state.round,
            "model_id": current_edge_state.model_id,
            "terminal_ledgers": ledgers_out,
            "updates_in_memory_by_round": updates_rows,
        }
    )


@router.post("/graceful_reset")
async def graceful_reset():
    """
    エッジサーバの学習状態を学習開始前に戻す。

    処理順:
      1. 現在の状態をディスクに保存（ログは継続して書き込まれている）
      2. 集約バケット・重複チェック・ローカルカウンタをクリア
      3. ラウンドスケジュールキャッシュをクリア
      4. edge_state を round=1 に戻してディスクに保存
    """
    reset_ts = time.strftime("%Y%m%d_%H%M%S")
    log.info(f"[admin] graceful_reset 要求を受信 ts={reset_ts}")

    # --- 1. 現在状態の保存 ---
    try:
        from edge_server.state import save_state, current_edge_state
        save_state(in_progress=False)
        log.info(f"[admin] 現在の状態を保存しました (round={current_edge_state.round})")
    except Exception:
        log.exception("[admin] 状態保存に失敗しました（リセットは続行）")

    try:
        from edge_server.utils.time_logger import edge_time_logger
        edge_time_logger.log_event("graceful_reset_initiated", {
            "ts": reset_ts,
            "round_before": getattr(current_edge_state, "round", None),
        })
    except Exception:
        pass

    prev_round = None

    # --- 2. 集約バケット・重複チェック・ローカルカウンタをクリア ---
    try:
        from edge_server.state import terminal_state_dicts, current_edge_state as ces
        prev_round = int(ces.round)
        terminal_state_dicts.clear()
        log.info("[admin] terminal_state_dicts をクリアしました")
    except Exception:
        log.exception("[admin] terminal_state_dicts のクリアに失敗")

    try:
        from edge_server.endpoints.terminal_update import seen_updates, terminal_local_counters
        seen_updates.clear()
        terminal_local_counters.clear()
        log.info("[admin] seen_updates / terminal_local_counters をクリアしました")
    except Exception:
        log.exception("[admin] seen_updates / terminal_local_counters のクリアに失敗")

    # --- 3. ラウンドスケジュールキャッシュ・集約状態をクリア ---
    try:
        from edge_server.endpoints.aggregation import (
            _scheduled_rounds, sent_rounds, in_progress_rounds, _pending_index
        )
        _scheduled_rounds.clear()
        sent_rounds.clear()
        in_progress_rounds.clear()
        _pending_index.clear()
        log.info("[admin] _scheduled_rounds / sent_rounds / in_progress_rounds / _pending_index をクリアしました")
    except Exception:
        log.exception("[admin] 集約キャッシュのクリアに失敗")

    # --- 4. edge_state をリセット ---
    try:
        from edge_server.state import current_edge_state, save_state, clear_round
        current_edge_state.round = 1
        current_edge_state.waiting_for_new_model = False
        current_edge_state.is_aggregating = False
        clear_round(1)
        save_state(in_progress=False)
        log.info("[admin] edge_state を round=1 にリセットしました")
    except Exception:
        log.exception("[admin] edge_state のリセットに失敗")

    try:
        from edge_server.utils.time_logger import edge_time_logger
        edge_time_logger.log_event("graceful_reset_complete", {
            "ts": reset_ts,
            "round_before": prev_round,
            "round_after": 1,
        })
    except Exception:
        pass

    log.info(f"[admin] graceful_reset 完了 (round {prev_round} → 1)")

    return JSONResponse(content={
        "status": "ok",
        "message": f"エッジサーバをリセットしました (round {prev_round} → 1)",
        "round_before": prev_round,
        "round_after": 1,
        "ts": reset_ts,
    })
