# central_server/state.py
import asyncio
from collections import OrderedDict, defaultdict
from typing import Dict, List, Any, Optional
import logging
import os
from datetime import datetime
from pathlib import Path
from central_server.utils.time_logger import central_time_logger
import json

# ロギング設定を統一
def configure_logging(level: Optional[str] = None) -> None:
    """
    中央サーバーのロギング設定を構成します。
    
    Args:
        level: ロギングレベル。None の場合はデフォルトの INFO が使用されます。
    """
    # メインのロガーを設定
    main_logger = logging.getLogger("central_server")
    main_logger.setLevel(level or logging.INFO)

    # ハンドラーがない場合のみ追加（重複を防ぐ）
    if not main_logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        main_logger.addHandler(handler)
        # Do not create a separate application log file here; time-records are written
        # by central_time_logger into 'logs/time_records'. Avoid creating
        # 'logs/central_server_app.log' to comply with centralized logging policy.

# ロギング設定を適用
configure_logging()

# テスト用のログ出力
logger = logging.getLogger("central_server")
logger.info("[中央] ロガー動作確認: 正常に記録できています。")

# Lock for thread-safe access to global structures in async context
state_dict_lock = asyncio.Lock()

# --- Edge registry (URLs or IDs of registered edge servers) ---
# 登録済みエッジサーバーの識別子集合（必要なら起動時に埋める/外部から管理）
edge_registry: set[str] = set()

# --- Global model storage ---
# Structure: { "round": int, "state_dict": OrderedDict(...) | None, "updated_at": float }
global_model_state: Dict[str, Any] = {
    "round": 0,
    "state_dict": None,
    "updated_at": None,
}

# --- Per-round updates collected from edges (in-memory bucket) ---
# Structure: { round_id: [ { "edge_id": str, "filename": str, "path": str, "n_samples": int, "saved_at": float }, ... ] }
round_updates: Dict[int, List[Dict[str, Any]]] = defaultdict(list)

# Lock for protecting round_updates map
round_updates_lock = asyncio.Lock()

# --- Convenience helpers for manipulating the above state safely ---

# 処理済みラウンドIDを記録するセット
processed_rounds = set()


async def add_round_update(round_id: int, entry: Dict[str, Any]) -> None:
    """
    Add an update record for the given round.
    Prevent duplicate entries for the same round.
    """
    try:
        async with round_updates_lock:
            # 重複チェック: 同一ラウンド内で同じ edge_id からの重複登録を防ぐ
            existing = round_updates.get(int(round_id), [])
            edge_id = entry.get("edge_id")
            logger.debug(f"重複チェック: round={round_id}, edge={edge_id}")
            # Handle both list and dict shapes for round_updates (legacy inconsistencies):
            if isinstance(existing, dict):
                if edge_id is not None and edge_id in existing:
                    logger.info("[中央] 重複のためスキップ: round=%s edge=%s", round_id, edge_id)
                    return
            else:
                if edge_id is not None and any((isinstance(e, dict) and e.get("edge_id") == edge_id) for e in existing):
                    logger.info("[中央] 重複のためスキップ: round=%s edge=%s", round_id, edge_id)
                    return

            logger.debug(f"エントリ追加: round={round_id}, edge={edge_id}")
            logger.info(
                f"[中央] エッジからデータ受信 data_received: round_id={round_id}, edge_id={entry.get('edge_id')}, n_samples={entry.get('n_samples')}"
            )
            try:
                central_time_logger.log_event("model_received", {
                    "round_id": round_id,
                    "edge_id": entry.get("edge_id"),
                    "n_samples": entry.get("n_samples"),
                })
            except Exception:
                logger.exception("model_received のタイムログ書き込みに失敗しました")

            # Append the entry for later aggregation. Do NOT mark the round as processed here;
            # processed_rounds should indicate rounds that have been integrated into the global model
            # (set by the integration flow), not simply received a first update.
            # Append or set depending on the current container type
            if isinstance(round_updates.get(int(round_id)), dict):
                # store minimal info keyed by edge_id to align with edge_update endpoint expectations
                if edge_id is None:
                    # fallback to generating a synthetic key
                    key = f"edge_unknown_{len(round_updates[int(round_id)])}"
                else:
                    key = edge_id
                round_updates[int(round_id)][key] = entry
            else:
                round_updates[int(round_id)].append(entry)
    except Exception as e:
        logger.error(f"[中央] add_round_update で例外: {str(e)}")


async def get_round_updates(round_id: int) -> List[Dict[str, Any]]:
    """Return a shallow copy of updates list for the round (empty list if none)."""
    async with round_updates_lock:
        items = list(round_updates.get(int(round_id), []))
    return items


async def clear_round_updates(round_id: int) -> None:
    """Remove all stored updates for a given round."""
    async with round_updates_lock:
        round_updates.pop(int(round_id), None)


async def set_global_model(round_id: int, state_dict: Any) -> None:
    """
    Replace the current global model with the provided state_dict and update round counter.
    The state_dict can be a torch state_dict or any serializable representation.
    """
    async with state_dict_lock:
        global_model_state["round"] = int(round_id)
        global_model_state["state_dict"] = state_dict
        global_model_state["updated_at"] = __import__("time").time()


async def get_global_model() -> Dict[str, Any]:
    """Return the global model state snapshot."""
    async with state_dict_lock:
        return dict(global_model_state)  # shallow copy


async def register_edge(edge_id: str) -> None:
    """Register an edge identifier (thread-safe from async code)."""
    # Not strictly necessary to lock a set, but do to be safe in async context.
    async with state_dict_lock:
        edge_registry.add(edge_id)


async def unregister_edge(edge_id: str) -> None:
    async with state_dict_lock:
        edge_registry.discard(edge_id)


# If you want to provide synchronous helper wrappers (optional)
def sync_get_round_updates(round_id: int) -> List[Dict[str, Any]]:
    """Synchronous utility for tests/scripts (not for use inside async event loop)."""
    return list(round_updates.get(int(round_id), []))


async def integrate_global_model(round_id: int) -> None:
    """
    Integrate the global model for the given round.
    """
    try:
        logger.info(f"[中央] 統合開始 integration_started: round_id={round_id}")
        try:
            central_time_logger.log_event("integration_started", {"round_id": round_id})
        except Exception:
            logger.exception("integration_started のタイムログ書き込みに失敗しました")
        # Delegate to existing endpoint utilities to perform FedAvg and write out the new batch.
        # Import here to avoid circular imports at module load time.
        from central_server.endpoints import edge_update as eu

        async with round_updates_lock:
            updates_for_round = round_updates.get(int(round_id))
            if not updates_for_round:
                logger.info(f"[中央] 統合対象の更新なし no_updates_to_integrate: round_id={round_id}")
                return

            # `eu.fedavg_multiple_edges` expects a mapping of edge_id -> update dict
            try:
                new_global = eu.fedavg_multiple_edges(updates_for_round)
            except Exception as e:
                logger.error(f"[中央] FedAvg 失敗 round={round_id}: {e}")
                raise

        # Persist the aggregated model to the dist directory and update meta using utilities in edge_update
        try:
            ts = __import__('time').strftime("%Y%m%d_%H%M%S")
            next_round = int(round_id) + 1
            batch_dir = eu.DIST_DIR / f"{ts}_r{next_round}"
            batch_dir.mkdir(parents=True, exist_ok=True)
            model_path = batch_dir / eu.DEFAULT_MODEL_NAME
            import torch
            # Offload heavy file write to thread to avoid blocking the event loop
            await asyncio.to_thread(torch.save, new_global, model_path)

            cfg_obj = {
                "round": next_round,
                "model_id": f"model-r{next_round}",
                "generated_at": ts,
                "notes": f"Aggregated from {len(updates_for_round)} edges for round {round_id}",
            }
            cfg_path = batch_dir / eu.DEFAULT_CONFIG_NAME
            import json
            cfg_path.write_text(json.dumps(cfg_obj, indent=2, ensure_ascii=False), encoding='utf-8')

            # Update meta and notify via logging
            meta = eu.load_meta()
            new_base_bytes = model_path.read_bytes()
            meta.update({
                "round": next_round,
                "model_id": cfg_obj["model_id"],
                "base_hash": eu.sha256_hex(new_base_bytes),
                "preferred_dtype": "torch_state_dict",
                "current_batch_rel": f"{batch_dir.name}",
                "model_rel": f"{batch_dir.name}/{eu.DEFAULT_MODEL_NAME}",
                "config_rel": f"{batch_dir.name}/{eu.DEFAULT_CONFIG_NAME}",
            })
            eu.save_meta(meta)

            # Switch central time logger to per-round directory
            try:
                from central_server.utils.time_logger import central_time_logger
                central_time_logger.set_round(next_round)
            except Exception:
                pass

            # Archive this round into a single directory under STORAGE_ROOT/archives/r<round>
            try:
                import shutil
                from central_server.config import STORAGE_ROOT, TRAINING_DATA_DIR
                archive_root = (STORAGE_ROOT / "archives" / f"r{next_round}").resolve()
                archive_root.mkdir(parents=True, exist_ok=True)
                # 1) Save meta snapshot
                try:
                    (archive_root / "current_meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
                except Exception:
                    logger.exception("アーカイブへのメタスナップショット書き込みに失敗しました")
                # 2) Copy dist batch to archive/dist
                try:
                    dest_dist = archive_root / "dist"
                    if dest_dist.exists():
                        shutil.rmtree(dest_dist, ignore_errors=True)
                    shutil.copytree(batch_dir, dest_dist)
                except Exception:
                    logger.exception("アーカイブへの dist バッチコピーに失敗しました")
                # 3) Copy per-round logs (both central and edge share logs/rounds/r<round>)
                try:
                    repo_logs_round = (Path(__file__).resolve().parent.parent.parent / "logs" / "rounds" / f"r{next_round}")
                    dest_logs = archive_root / "logs"
                    if repo_logs_round.exists():
                        if dest_logs.exists():
                            shutil.rmtree(dest_logs, ignore_errors=True)
                        shutil.copytree(repo_logs_round, dest_logs)
                except Exception:
                    logger.exception("アーカイブへのラウンドログコピーに失敗しました")
                # 4) Write filtered training data index for this round
                try:
                    index_src = TRAINING_DATA_DIR / "index.jsonl"
                    out_index = archive_root / "training_index.jsonl"
                    if index_src.exists():
                        with open(index_src, "r", encoding="utf-8") as fin, open(out_index, "w", encoding="utf-8") as fout:
                            for line in fin:
                                try:
                                    it = json.loads(line)
                                    if int(it.get("round", -1)) == next_round:
                                        fout.write(json.dumps(it, ensure_ascii=False) + "\n")
                                except Exception:
                                    continue
                except Exception:
                    logger.exception("アーカイブへの学習データインデックス書き込みに失敗しました")
                # 5) Minimal README for archive
                try:
                    (archive_root / "README.txt").write_text(
                        f"Round r{next_round} archive\n\n"
                        f"Contains: dist batch, meta snapshot, per-round logs, filtered training index.\n",
                        encoding='utf-8'
                    )
                except Exception:
                    pass
            except Exception:
                logger.exception("ラウンドアーカイブの作成に失敗しました")

            logger.info(
                f"[中央] 統合完了 integration_completed: round_id={round_id}, next_round={next_round}, batch={batch_dir.name}"
            )
            try:
                central_time_logger.log_event("integration_completed", {"round_id": round_id, "next_round": next_round, "batch": batch_dir.name})
            except Exception:
                logger.exception("integration_completed のタイムログ書き込みに失敗しました")
        except Exception as e:
            logger.error(f"[中央] 統合モデルの保存に失敗 round={round_id}: {e}")
            raise
    except Exception as e:
        logger.error(f"[中央] integrate_global_model で例外: {str(e)}")


async def distribute_global_model(round_id: int) -> None:
    """
    Distribute the global model to all edges for the given round.
    """
    try:
        logger.info(f"[中央] 配布開始 distribution_started: round_id={round_id}")
        try:
            central_time_logger.log_event("distribution_started", {"round_id": round_id})
        except Exception:
            logger.exception("distribution_started のタイムログ書き込みに失敗しました")
        # Use existing dist directory and meta to determine batch to distribute.
        from central_server.endpoints import edge_update as eu

        meta = eu.load_meta()
        batch_rel = meta.get("current_batch_rel")
        if not batch_rel:
            logger.warning(f"[中央] 配布するバッチなし no_batch_to_distribute round={round_id}")
            return

        batch_dir = eu.DIST_DIR / batch_rel
        model_path = batch_dir / eu.DEFAULT_MODEL_NAME
        if not model_path.exists():
            logger.error(f"[中央] モデルファイルが見つかりません: {model_path}")
            return

        # In this simple implementation we mark distribution as complete and rely on edges pulling from dist URLs
        logger.info(f"[中央] 配布済み model_distributed: round_id={round_id}, model_path={model_path.as_posix()}")
        try:
            central_time_logger.log_event("model_distributed", {"round_id": round_id, "model_path": model_path.as_posix()})
        except Exception:
            logger.exception("model_distributed のタイムログ書き込みに失敗しました")
    except Exception as e:
        logger.error(f"[中央] distribute_global_model で例外: {str(e)}")


# エッジからデータを受信したとき
async def receive_data_from_edge(data):
    logger.info(f"エッジからデータ受信: {data}")
    # Expecting a dict with keys: edge_id, round, num_clients, sum_n_samples, state_dict (optional) or file_path
    try:
        edge_id = data.get("edge_id")
        round_id = int(data.get("round"))
        n_clients = int(data.get("num_clients", 0))
        sum_n = int(data.get("sum_n_samples", 0))

        async with round_updates_lock:
            if round_id not in round_updates or not isinstance(round_updates[round_id], dict):
                round_updates[round_id] = {}

            entry = round_updates[round_id].get(edge_id, {})
            # merge provided state_dict or path into the entry
            if "state_dict" in data:
                entry["state_dict"] = data["state_dict"]
            if "file_path" in data:
                entry["file_path"] = data["file_path"]

            entry.setdefault("num_clients", n_clients)
            entry.setdefault("sum_n_samples", sum_n)
            round_updates[round_id][edge_id] = entry

        # Log and signal that we have received an update
        await add_round_update(round_id, {"edge_id": edge_id, "n_samples": n_clients})
    except Exception as e:
        logger.error(f"[中央] receive_data_from_edge エラー: {e}")


# 中央の統合が完了したとき
async def complete_integration():
    logger.info("統合が正常に完了しました。")
    # After integration we may want to clear stored updates and advance state.
    try:
        # find the latest processed round(s) and clear their updates
        async with round_updates_lock:
            rounds = list(round_updates.keys())
        for r in rounds:
            # clear data for rounds that have been processed
            if r in processed_rounds:
                await clear_round_updates(r)
                logger.info(f"[中央] round={r} の更新をクリアしました")
    except Exception as e:
        logger.error(f"[中央] complete_integration エラー: {e}")


# モデルを配布したタイミング
async def distribute_model(model):
    logger.info(f"モデル配布: {model}")
    try:
        # model can be a path or metadata dict; attempt to call distribute_global_model if numeric round present
        round_id = None
        if isinstance(model, dict):
            round_id = model.get("round")
        elif isinstance(model, (int, str)):
            try:
                round_id = int(model)
            except Exception:
                round_id = None

        if round_id is not None:
            await distribute_global_model(round_id)
        else:
            logger.info("[中央] distribute_model: round_id なしのため distribute_global_model はスキップ")
    except Exception as e:
        logger.error(f"[中央] distribute_model エラー: {e}")


class RoundState:
    def __init__(self, state_file="round_state.json"):
        self.round_states = {}
        self.state_file = Path(state_file)
        self.load_state()

    def is_round_completed(self, round_id):
        return self.round_states.get(round_id) == "completed"

    def mark_round_in_progress(self, round_id):
        self.round_states[round_id] = "in_progress"

    def mark_round_completed(self, round_id):
        self.round_states[round_id] = "completed"

    def save_state(self):
        with self.state_file.open("w", encoding="utf-8") as f:
            json.dump(self.round_states, f, indent=2)

    def load_state(self):
        if self.state_file.exists():
            with self.state_file.open("r", encoding="utf-8") as f:
                self.round_states = json.load(f)
