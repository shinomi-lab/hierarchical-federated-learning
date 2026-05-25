# edge_server/background_tasks.py
import asyncio
import logging
import time
import os
from pathlib import Path

from edge_server.config import CENTRAL_SERVER_URL
from edge_server.state import current_edge_state
# notification helper (WebSocket) to inform connected devices
from edge_server.endpoints.notifications import broadcast_model_update
from edge_server.startup import (
    fetch_json,
    fetch_global_model,
    fetch_training_data,
    _join,
    BASE_DIR,
    find_bin_meta_for_model_rel,
)
from edge_server.utils.time_logger import edge_time_logger

log = logging.getLogger("edge.background")
log.setLevel(logging.DEBUG)

# Polling interval can be tuned via env var EDGE_POLL_INTERVAL_SECONDS (seconds).
# Default is imported from config.py
try:
    from edge_server.config import POLLING_INTERVAL_SECONDS, DEFAULT_MODEL_NAME
except ImportError:
    POLLING_INTERVAL_SECONDS = 5
    DEFAULT_MODEL_NAME = "global_model_mobile.pt"


async def poll_central_server_for_updates():
    """
    中央サーバーを定期的にポーリングして、新しいラウンドのモデルがないか確認する。
    集約中または新モデル待機中はポーリングをスキップして無限ループを防ぐ。
    """
    log.info("Starting background poller for central server updates...")
    log.info(f"Polling interval: {POLLING_INTERVAL_SECONDS}s")
    # Small startup delay to allow other startup handlers (initial state/bootstrap)
    # to complete. This avoids a race where the poller queries central before the
    # local startup init has set the correct current_edge_state, causing duplicate
    # work such as re-downloading the same model immediately after startup.
    await asyncio.sleep(1)
    while True:
        try:
            # 集約中または新モデル待機中はポーリングをスキップ
            if current_edge_state.is_aggregating:
                log.debug(f"Skipping poll: aggregation in progress for round {current_edge_state.round}")
                await asyncio.sleep(POLLING_INTERVAL_SECONDS)
                continue

            if current_edge_state.waiting_for_new_model:
                log.debug(f"Polling for new model after aggregation (current round: {current_edge_state.round})")
                # 待機中でもポーリングは続行（新しいラウンドを検出するため）

            # 1. 中央サーバーから最新のメタ情報を取得
            info_url = _join(CENTRAL_SERVER_URL, "get_global_model")
            # fetch_json is synchronous; offload to thread to avoid blocking the event loop
            central_info = await asyncio.to_thread(fetch_json, info_url, 10)
            central_config = central_info.get("config", {})
            central_round = central_config.get("round")

            # 2. ラウンド番号を比較
            if central_round and central_round > current_edge_state.round:
                log.info(f"New round {central_round} detected on central server (current is {current_edge_state.round}). Fetching updates...")

                # 3. 新しいモデルと設定をダウンロード
                ts = time.strftime("%Y%m%d_%H%M%S")
                save_dir = BASE_DIR / ts
                save_dir.mkdir(parents=True, exist_ok=True)

                # offload heavy network/disk work to thread so poller doesn't block the loop
                await asyncio.to_thread(fetch_global_model, central_info, save_dir)
                # config (app.json) is no longer provided by central; skip fetch_config
                await asyncio.to_thread(fetch_training_data, central_info, save_dir)

                # 3b. Attempt to auto-convert downloaded .pt -> weight.bin + meta.json
                try:
                    # locate tools script at repository root: ../../tools/pt_to_meta_weights.py
                    tools_script = Path(__file__).resolve().parents[1] / "tools" / "pt_to_meta_weights.py"
                    pt_path = save_dir / DEFAULT_MODEL_NAME

                    try:
                        edge_time_logger.log_event("poller_auto_convert_started", {"batch": ts, "save_dir": str(save_dir)})
                    except Exception:
                        pass

                    # If converter script missing, skip auto-convert
                    if not tools_script.exists():
                        log.warning("Converter script not found at %s, skipping auto-convert", str(tools_script))
                        try:
                            edge_time_logger.log_warn("poller_auto_convert_missing", {"batch": ts, "path": str(tools_script)})
                        except Exception:
                            pass
                        rc, sout, serr = 254, "", "converter-not-found"
                    else:
                        def _run_converter():
                            import subprocess, sys
                            cmd = [sys.executable, str(tools_script), str(pt_path), str(save_dir), "--model-version", ts]
                            try:
                                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                                return proc.returncode, proc.stdout, proc.stderr
                            except Exception as e:
                                return 255, "", repr(e)

                        rc, sout, serr = await asyncio.to_thread(_run_converter)
                    if rc != 0:
                        try:
                            edge_time_logger.log_error("poller_auto_convert_failed", {"batch": ts, "rc": rc, "stderr": (serr or "")[:200]})
                        except Exception:
                            pass
                        log.warning("Auto-convert failed for %s (rc=%s) stderr=%s", save_dir.name, rc, (serr or "")[:200])
                    else:
                        try:
                            edge_time_logger.log_event("poller_auto_converted", {"batch": ts, "stdout": (sout or "")[:200]})
                        except Exception:
                            pass
                        log.info("Auto-convert succeeded for %s", save_dir.name)
                except Exception:
                    log.exception("converter invocation failed")

                # 4. エッジサーバーの状態を更新
                current_edge_state.update(central_config)
                # Log that model+config+data are ready on disk and state updated
                try:
                    bin_rel, meta_rel = find_bin_meta_for_model_rel(f"{save_dir.name}/{DEFAULT_MODEL_NAME}")
                    edge_time_logger.log_event("model_ready_for_devices", {
                        "model_id": current_edge_state.model_id,
                        "round": current_edge_state.round,
                        "download_rel": bin_rel,
                    })
                except Exception:
                    pass

                # After updating local state, notify connected devices via WebSocket
                try:
                    # Only broadcast when weight.bin exists; do NOT send .pt to devices
                    bin_rel, meta_rel = find_bin_meta_for_model_rel(f"{save_dir.name}/{DEFAULT_MODEL_NAME}")
                    if not bin_rel:
                        log.info("Skipping device notification: weight.bin not present for %s", save_dir.name)
                    else:
                        metadata = {
                            "model_id": current_edge_state.model_id,
                            "round": current_edge_state.round,
                            "timestamp": time.strftime("%Y%m%d_%H%M%S"),
                            "description": central_config.get("description") if isinstance(central_config, dict) else None,
                            # provide explicit bin + meta rels
                            "model_bin_rel": bin_rel,
                            "model_meta_rel": meta_rel,
                        }
                        try:
                            nid = await broadcast_model_update(metadata)
                            log.info(f"Notified devices with notification_id={nid}")
                        except Exception:
                            log.exception("failed to broadcast model update to websocket clients")
                except Exception:
                    log.exception("failed to broadcast model update to websocket clients")
        except Exception as e:
            log.error(f"Failed to poll central server for updates: {e}", exc_info=True)

        await asyncio.sleep(POLLING_INTERVAL_SECONDS)