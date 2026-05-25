# central_server/endpoints/edge_update.py
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from pathlib import Path
import io, os, time, json, hashlib, sqlite3
from typing import Optional, Dict, Tuple, List, Any
import asyncio
import httpx
import torch
from central_server.utils.time_logger import central_time_logger
from central_server.utils import marker_backend
from central_server.utils.marker_store import _normalize_federation_run
import subprocess
import sys
from pathlib import Path
import logging, traceback
from datetime import datetime
import aiofiles
import uuid

logger = logging.getLogger(__name__)


router = APIRouter()
from central_server.state import round_updates, round_updates_lock
from central_server.config import CENTRAL_AGGREGATION_THRESHOLD

# ========= サーバ内の配置（あなたのプロジェクト構成に合わせて調整） =========
def _choose_storage_paths():
    """Choose safe on-disk storage directories.

    Priority:
    1. If env HFL_STORAGE_DIR is set, use it as storage root.
    2. Otherwise, use repo-local 'received_edges' and 'dist'.
       If those resolve into a OneDrive path, fall back to user home 'hfl_data' to avoid OneDrive/Sync delays.
    """
    # Use project-root state directory from central_server.config for META_PATH to
    # ensure all modules read/write the same authoritative meta file.
    from central_server.config import META_PATH as CONFIG_META_PATH
    repo_root = Path(__file__).resolve().parent.parent.parent
    default_received = (repo_root / "received_edges").resolve()
    default_dist = (repo_root / "dist").resolve()

    env_root = os.environ.get("HFL_STORAGE_DIR")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        received = (root / "received_edges").resolve()
        dist = (root / "dist").resolve()
        meta = (root / "state" / "current_meta.json").resolve()
        return received, dist, meta

    # detect OneDrive in path and avoid using it for heavy IO
    def _is_onedrive(p: Path) -> bool:
        try:
            return "onedrive" in str(p).lower() or "sharepoint" in str(p).lower()
        except Exception:
            return False

    if _is_onedrive(default_received) or _is_onedrive(default_dist) or _is_onedrive(CONFIG_META_PATH):
        # fallback to home directory under hfl_data for ALL storage including meta
        home_root = Path.home() / "hfl_data"
        received = (home_root / "received_edges").resolve()
        dist = (home_root / "dist").resolve()
        meta = (home_root / "state" / "current_meta.json").resolve()
        return received, dist, meta

    return default_received, default_dist, CONFIG_META_PATH.resolve()


# choose storage paths
BASE_DIR, DIST_DIR, META_PATH = _choose_storage_paths()
BASE_DIR = BASE_DIR.resolve()
DIST_DIR = DIST_DIR.resolve()
META_PATH = META_PATH.resolve()
# log chosen storage
logger.info(f"ストレージパス: BASE_DIR={BASE_DIR}, DIST_DIR={DIST_DIR}, META_PATH={META_PATH}")
from central_server.config import DEFAULT_MODEL_NAME
DEFAULT_CONFIG_NAME = "app.json"

# Persistent store for received payload hashes to support idempotency
RECEIVED_HASHES_FILE = BASE_DIR / "received_hashes.json"
# Persistent mapping for (round -> { edge_id: sha }) to implement first-wins per-edge-per-round
RECEIVED_BY_EDGE_FILE = BASE_DIR / "received_by_edge.json"


def _load_received_hashes() -> set:
    try:
        if RECEIVED_HASHES_FILE.exists():
            txt = RECEIVED_HASHES_FILE.read_text(encoding="utf-8")
            data = json.loads(txt)
            if isinstance(data, list):
                return set(data)
    except Exception:
        pass
    return set()


def _save_received_hashes(hashes: set) -> None:
    try:
        tmp = RECEIVED_HASHES_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(sorted(list(hashes)), ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(RECEIVED_HASHES_FILE)
    except Exception:
        # best-effort
        pass


# load on import
_received_hashes = _load_received_hashes()
_hash_lock = asyncio.Lock()


def _load_received_by_edge() -> Dict[str, Dict[str, str]]:
    try:
        if RECEIVED_BY_EDGE_FILE.exists():
            txt = RECEIVED_BY_EDGE_FILE.read_text(encoding="utf-8")
            data = json.loads(txt)
            if isinstance(data, dict):
                # ensure nested dicts
                return {k: dict(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save_received_by_edge(mapping: Dict[str, Dict[str, str]]) -> None:
    try:
        tmp = RECEIVED_BY_EDGE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(RECEIVED_BY_EDGE_FILE)
    except Exception:
        # best-effort
        pass


_received_by_edge = _load_received_by_edge()

# initialize sqlite-backed marker DB for faster duplicate checks when using sqlite
try:
    from central_server.utils import marker_store as _marker_store
    _marker_store.init_marker_db()
except Exception:
    # best-effort: continue if DB/redis backend can't be initialized
    pass

BASE_DIR.mkdir(parents=True, exist_ok=True)
DIST_DIR.mkdir(parents=True, exist_ok=True)
META_PATH.parent.mkdir(parents=True, exist_ok=True)

# ============ 現行メタの読み書き ============
def load_meta() -> Dict:
    if META_PATH.exists():
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    # 初期値（まだ配布していない状態）
    meta = {
        "round": 1,
        "model_id": "demo-mlp-v1",
        "base_hash": "sha256:bootstrap",
        "preferred_dtype": "torch_state_dict",
        "upload_endpoint": "/receive_terminal_weights/{terminal_id}",
        "aggregation_threshold": 1,
        "expected_param_len": 0,
        "order_version": 1,
    }
    META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta

def save_meta(meta: Dict) -> None:
    # Write primary/meta file (project-level state directory)
    tmp = META_PATH.with_suffix('.json.tmp')
    try:
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(META_PATH)
    except Exception as e:
        # Log failure and attempt best-effort fallback
        logger.error(f"メタの原子書き込みに失敗しました {META_PATH}: {e}")
        try:
            META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info(f"フォールバックでメタを直接書き込み保存しました {META_PATH}")
        except Exception as e2:
            logger.exception(f"フォールバックでもメタ保存に失敗しました: {e2}")

    # Also write a mirrored copy inside the package-local central_server/state
    try:
        local_state_dir = Path(__file__).resolve().parent.parent / 'state'
        local_state_dir.mkdir(parents=True, exist_ok=True)
        local_meta = local_state_dir / 'current_meta.json'
        tmp2 = local_meta.with_suffix('.json.tmp')
        try:
            tmp2.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp2.replace(local_meta)
            logger.debug(f"ミラー先メタを保存しました {local_meta}")
        except Exception as e:
            logger.warning(f"ミラー先メタの原子保存に失敗しました: {e}")
            local_meta.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info(f"フォールバックでミラー先メタを保存しました {local_meta}")
    except Exception as e:
        # non-fatal: continue if mirrored write fails but log it
        logger.warning(f"ミラー先メタコピーの保存に失敗しました: {e}")

def sha256_hex(b: bytes) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(b).hexdigest()

# ============ キー整合チェック（任意で厳格化） ============
def check_state_dict_keys(sd: dict) -> None:
    if not isinstance(sd, dict) or not sd:
        raise HTTPException(422, detail="state_dict が空または辞書ではありません")
    # 任意: 許容キーや必須キーをチェックしたい場合はここで

# ============ グローバルモデル更新の基本方針 ============
# 今回は「届いた集約重み = そのまま新グローバル」として採用（MVP）
def build_next_global_from_agg(agg_sd: dict) -> dict:
    # 追加の正則化/スムージング等を挟むならここに
    return agg_sd

def _check_same_keys_and_shapes(sds: List[dict]) -> List[str]:
    if not sds: raise ValueError("empty state_dicts")
    keys = set(sds[0].keys())
    for i, sd in enumerate(sds[1:], 1):
        if set(sd.keys()) != keys: raise ValueError(f"key mismatch at #{i}")
    for k in keys:
        shape0 = sds[0][k].shape
        for i, sd in enumerate(sds[1:], 1):
            if sd[k].shape != shape0: raise ValueError(f"shape mismatch at key {k}")
    return list(keys)

def _sanitize_weights(ns: List[int]) -> List[float]:
    ws = [float(max(0, n)) for n in ns]
    s = sum(ws)
    if s <= 0: return [1.0 / len(ws)] * len(ws)
    return [w / s for w in ws]

def _has_nan_inf(state_dict: dict) -> bool:
    """Return True if any floating-point tensor in state_dict contains NaN or Inf."""
    for t in state_dict.values():
        if torch.is_floating_point(t):
            if torch.isnan(t).any() or torch.isinf(t).any():
                return True
    return False


def fedavg_multiple_edges(updates: Dict[str, Dict[str, Any]]) -> Dict:
    """
    複数のエッジからの更新を FedAvg で統合する。
    重み付けは各エッジが集約したクライアントの総サンプル数 (sum_n_samples) を使う。
    NaN/Inf を含む更新は集約から除外する（0埋め置換は行わない）。
    """
    if not updates:
        raise ValueError("No updates to aggregate")

    # NaN/Inf を含む更新を除外する
    valid_items = []
    for edge_id, item in updates.items():
        if _has_nan_inf(item["state_dict"]):
            logger.warning(f"エッジ '{edge_id}' の更新に NaN/Inf を検出しました。集約から除外します")
            try:
                central_time_logger.log_event("nan_in_edge_update_excluded", {"edge_id": edge_id})
            except Exception:
                pass
        else:
            valid_items.append(item)

    if not valid_items:
        raise ValueError("All edge updates contained NaN/Inf; cannot aggregate")

    state_dicts = [item["state_dict"] for item in valid_items]
    # 重み付けには、各エッジが担当したクライアントのサンプル数の合計を使う
    weights_for_avg = [item["sum_n_samples"] for item in valid_items]

    keys = _check_same_keys_and_shapes(state_dicts)
    sanitized_weights = _sanitize_weights(weights_for_avg)

    new_global = {}
    for k in keys:
        if torch.is_floating_point(state_dicts[0][k]):
            acc = torch.zeros_like(state_dicts[0][k], dtype=torch.float32)
            for i, sd in enumerate(state_dicts):
                t = sd[k]
                acc.add_(t, alpha=sanitized_weights[i])
            new_global[k] = acc
        else:
            # floatでないテンソル（例: num_batches_tracked）は最初のものを採用
            new_global[k] = state_dicts[0][k].clone()
    return new_global

# ============ 応答に配るURL（相対で返す例） ============
def dist_links(batch: str, request: Request) -> Dict[str, str]:
    scheme = request.url.scheme
    host = request.headers.get("host") or f"{request.client.host}"
    base_url = f"{scheme}://{host}"
    return {
        "model":  f"{base_url}/download_model/{batch}/{DEFAULT_MODEL_NAME}",
        "config": f"{base_url}/download_config/{batch}/{DEFAULT_CONFIG_NAME}",
        # データ等あればここに追加
    }


async def _background_aggregate(round_id: int, updates: Dict[str, Dict[str, Any]], request: Request) -> None:
    """
    Perform FedAvg aggregation and persist the new global model in background.
    This isolates heavy CPU/IO from the HTTP request thread so /edge_update can
    return immediately.
    """
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        print("\n" + "="*50)
        print(f"📦 (バックグラウンド) ラウンド {round_id} を {len(updates)} エッジ分集約中")
        logger.info(f"[edge_update] バックグラウンド集約開始 round={round_id}, エッジ数={len(updates)}")

        central_time_logger.log_event("aggregation_started", {"round": round_id, "collected_edges": len(updates)})

        # diagnostic logging of keys/shapes
        try:
            for eid, item in updates.items():
                sd = item.get("state_dict")
                if isinstance(sd, dict):
                    logger.info(f"[edge_update-bg] edge={eid} の state_dict キー: {sorted(list(sd.keys()))}")
                    shapes = {k: (tuple(v.shape) if hasattr(v, 'shape') else str(type(v))) for k, v in list(sd.items())[:10]}
                    logger.info(f"[edge_update-bg] edge={eid} のサンプル形状: {shapes}")
        except Exception:
            logger.exception("バックグラウンドで state_dict の形状/キー記録に失敗しました")

        # Offload heavy CPU/IO to a thread to avoid blocking the event loop
        def _sync_aggregate_and_persist(updates_local: Dict[str, Dict[str, Any]], round_local: int, ts_local: str):
            try:
                # run FedAvg (CPU-bound)
                try:
                    new_global_local = fedavg_multiple_edges(updates_local)
                except ValueError as e:
                    logger.error(f"FedAvg 集約失敗: {e}。ラウンド {round_local} の更新をスキップします。")
                    try:
                        central_time_logger.log_event("aggregation_failed_skipped", {"round": round_local, "reason": str(e)})
                    except Exception:
                        pass
                    return

                # write new batch (atomic)
                next_round_local = int(round_local) + 1
                batch_dir_local = (DIST_DIR / f"{ts_local}_r{next_round_local}")
                batch_dir_local.mkdir(parents=True, exist_ok=True)
                model_path_local = batch_dir_local / DEFAULT_MODEL_NAME
                try:
                    tmp_model_local = model_path_local.with_suffix('.pt.tmp')
                    torch.save(new_global_local, tmp_model_local)
                    tmp_model_local.replace(model_path_local)
                    logger.info("バッチモデルを書き込みました: %s (存在=%s)", str(model_path_local.resolve()), model_path_local.exists())
                except Exception:
                    logger.exception("バックグラウンドでモデルの原子保存に失敗。直接保存を試行します")
                    torch.save(new_global_local, model_path_local)
                    logger.info("フォールバックでバッチモデルを書き込みました: %s (存在=%s)", str(model_path_local.resolve()), model_path_local.exists())

                # Attempt to generate meta.json and weight.bin for terminal distribution
                try:
                    script = Path(__file__).resolve().parent.parent / 'tools' / 'pt_to_meta_weights.py'
                    if script.exists():
                        subprocess.run([sys.executable, str(script), str(model_path_local), str(batch_dir_local), '--model-version', ts_local], check=False)
                        logger.info("pt_to_meta_weights をバッチ %s に対して実行しました", batch_dir_local.name)
                    else:
                        logger.info("pt_to_meta_weights が見つかりません。バッチ %s の変換をスキップします", batch_dir_local.name)
                except Exception:
                    logger.exception("バッチ %s で pt_to_meta_weights の呼び出しに失敗しました", batch_dir_local.name)

                cfg_path_local = batch_dir_local / DEFAULT_CONFIG_NAME
                cfg_obj_local = {
                    "round": next_round_local,
                    "model_id": None,
                    "generated_at": ts_local,
                    "notes": f"Aggregated from {len(updates_local)} edges in round {round_local}",
                }
                try:
                    tmp_cfg_local = cfg_path_local.with_suffix('.json.tmp')
                    tmp_cfg_local.write_text(json.dumps(cfg_obj_local, indent=2, ensure_ascii=False), encoding="utf-8")
                    tmp_cfg_local.replace(cfg_path_local)
                except Exception:
                    logger.exception("バックグラウンドで設定の原子保存に失敗。直接書き込みを試行します")
                    cfg_path_local.write_text(json.dumps(cfg_obj_local, indent=2, ensure_ascii=False), encoding="utf-8")

                # --- Promote latest model/config to canonical repo-level paths first
                promoted_to_canonical = False
                try:
                    from central_server.config import GLOBAL_MODEL_PATH, CONFIG_PATH
                    # ensure parent exists and copy model
                    try:
                        GLOBAL_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
                        tmp_global = GLOBAL_MODEL_PATH.with_suffix('.pt.tmp')
                        tmp_global.write_bytes(model_path_local.read_bytes())
                        tmp_global.replace(GLOBAL_MODEL_PATH)
                        promoted_to_canonical = True
                        logger.info("モデルを正規パスへ昇格しました: %s (存在=%s)", str(GLOBAL_MODEL_PATH.resolve()), GLOBAL_MODEL_PATH.exists())
                    except Exception:
                        try:
                            GLOBAL_MODEL_PATH.write_bytes(model_path_local.read_bytes())
                            promoted_to_canonical = True
                            logger.info("フォールバック書き込みでモデルを正規パスへ昇格しました: %s (存在=%s)", str(GLOBAL_MODEL_PATH.resolve()), GLOBAL_MODEL_PATH.exists())
                        except Exception:
                            logger.exception("バッチモデルを GLOBAL_MODEL_PATH へコピーできませんでした")
                    # copy config
                    try:
                        tmp_cfg_repo = CONFIG_PATH.with_suffix('.json.tmp')
                        tmp_cfg_repo.write_text(json.dumps(cfg_obj_local, indent=2, ensure_ascii=False), encoding='utf-8')
                        tmp_cfg_repo.replace(CONFIG_PATH)
                        logger.info("設定を正規パスへ昇格しました: %s (存在=%s)", str(CONFIG_PATH.resolve()), CONFIG_PATH.exists())
                    except Exception:
                        try:
                            CONFIG_PATH.write_text(json.dumps(cfg_obj_local, indent=2, ensure_ascii=False), encoding='utf-8')
                            logger.info("フォールバック書き込みで設定を正規パスへ昇格しました: %s (存在=%s)", str(CONFIG_PATH.resolve()), CONFIG_PATH.exists())
                        except Exception:
                            logger.exception("バッチ設定を CONFIG_PATH へコピーできませんでした")
                except Exception:
                    logger.exception("バッチ成果物をリポジトリ正規パスへコピーできませんでした")

                # update meta AFTER promotion so clients see a reachable canonical path
                meta_local = load_meta()
                # prefer canonical bytes if promotion succeeded
                try:
                    if promoted_to_canonical and GLOBAL_MODEL_PATH.exists():
                        new_base_bytes_local = GLOBAL_MODEL_PATH.read_bytes()
                    else:
                        new_base_bytes_local = model_path_local.read_bytes()
                except Exception:
                    new_base_bytes_local = model_path_local.read_bytes()
                meta_local.update({
                    "round": next_round_local,
                    "model_id": cfg_obj_local["model_id"] or meta_local.get("model_id", "unknown"),
                    "base_hash": sha256_hex(new_base_bytes_local),
                    "preferred_dtype": "torch_state_dict",
                    "current_batch_rel": f"{batch_dir_local.name}",
                    "model_rel": f"{batch_dir_local.name}/{DEFAULT_MODEL_NAME}",
                    "config_rel": f"{batch_dir_local.name}/{DEFAULT_CONFIG_NAME}",
                })
                save_meta(meta_local)

                # also prepare in-memory bytes for immediate push (avoid waiting for disk copy)
                try:
                    buf = io.BytesIO()
                    torch.save(new_global_local, buf)
                    buf.seek(0)
                    model_bytes_for_push = buf.read()
                except Exception:
                    model_bytes_for_push = None
                try:
                    cfg_bytes_for_push = json.dumps(cfg_obj_local, ensure_ascii=False).encode('utf-8')
                except Exception:
                    cfg_bytes_for_push = None

                central_time_logger.log_event("aggregation_completed", {"round": round_local, "next_round": next_round_local})
                logger.info(f"[edge_update] バックグラウンド集約完了 round={round_local}, バッチ={batch_dir_local}")

                # [AI] ラウンド完了レポート（ノンブロッキング）
                try:
                    from central_server.ai_advisor import get_advisor
                    advisor = get_advisor()
                    if advisor:
                        asyncio.create_task(advisor.on_round_complete(round_local))
                except Exception:
                    pass
                # --- Promote latest model/config to canonical repo-level paths so
                # registered edges can be pushed updates immediately. This copies
                # the generated batch artifacts into GLOBAL_MODEL_PATH / CONFIG_PATH
                try:
                    from central_server.config import GLOBAL_MODEL_PATH, CONFIG_PATH
                    # copy model
                    try:
                        # ensure parent exists
                        GLOBAL_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
                        tmp_global = GLOBAL_MODEL_PATH.with_suffix('.pt.tmp')
                        tmp_global.write_bytes(model_path_local.read_bytes())
                        tmp_global.replace(GLOBAL_MODEL_PATH)
                        logger.info("モデルを正規パスへ昇格しました: %s (存在=%s)", str(GLOBAL_MODEL_PATH.resolve()), GLOBAL_MODEL_PATH.exists())
                    except Exception:
                        # fallback direct write
                        GLOBAL_MODEL_PATH.write_bytes(model_path_local.read_bytes())
                        logger.info("フォールバック書き込みでモデルを正規パスへ昇格しました: %s (存在=%s)", str(GLOBAL_MODEL_PATH.resolve()), GLOBAL_MODEL_PATH.exists())
                    # copy config
                    try:
                        tmp_cfg_repo = CONFIG_PATH.with_suffix('.json.tmp')
                        tmp_cfg_repo.write_text(json.dumps(cfg_obj_local, indent=2, ensure_ascii=False), encoding='utf-8')
                        tmp_cfg_repo.replace(CONFIG_PATH)
                        logger.info("設定を正規パスへ昇格しました: %s (存在=%s)", str(CONFIG_PATH.resolve()), CONFIG_PATH.exists())
                    except Exception:
                        CONFIG_PATH.write_text(json.dumps(cfg_obj_local, indent=2, ensure_ascii=False), encoding='utf-8')
                        logger.info("フォールバック書き込みで設定を正規パスへ昇格しました: %s (存在=%s)", str(CONFIG_PATH.resolve()), CONFIG_PATH.exists())
                except Exception:
                    logger.exception("バッチ成果物をリポジトリ正規パスへコピーできませんでした")

                # return bytes for push
                return {"model_bytes": model_bytes_for_push, "config_bytes": cfg_bytes_for_push}
            except Exception:
                logger.exception("_sync_aggregate_and_persist が失敗しました")

        # Run sync aggregation/persist in thread and return model/config bytes for immediate push
        result = await asyncio.to_thread(_sync_aggregate_and_persist, updates, round_id, ts)

        # Schedule async push using in-memory bytes to avoid waiting for disk copies
        try:
            from central_server.endpoints import edge_management as em
            model_bytes = None
            config_bytes = None
            if isinstance(result, dict):
                model_bytes = result.get('model_bytes')
                config_bytes = result.get('config_bytes')
            # schedule background async push; pass bytes if available
            async def _run_push_and_log():
                try:
                    await em.async_send_model_and_app(model_bytes=model_bytes, config_bytes=config_bytes)
                except Exception as e:
                    try:
                        central_time_logger.log_event("background_task_exception", {"task": "async_send_model_and_app_from_edge_update", "error": str(e)})
                    except Exception:
                        pass
                    logger.exception("edge_update からのバックグラウンドプッシュタスクが失敗しました")

            asyncio.create_task(_run_push_and_log())
        except Exception:
            logger.exception("登録エッジへの非同期プッシュのスケジュールに失敗しました")

    except Exception as e:
        tb = traceback.format_exc()
        logger.exception(f"バックグラウンド集約が失敗しました round={round_id}: {e}\n{tb}")
        central_time_logger.log_event("aggregation_failed", {"round": round_id, "error": str(e)})
        # we don't raise: failures are recorded and operators can inspect logs


@router.post("/edge_update")
async def edge_update(
    request: Request,
    edge_id: str = Form(...),
    terminal_id: Optional[str] = Form(None),
    round: int = Form(...),
    num_clients: int = Form(...),
    sum_n_samples: int = Form(...),
    # 任意メタ（あれば受ける）
    model_id: Optional[str] = Form(None),
    base_hash: Optional[str] = Form(None),
    # Optional event timestamp provided by edge (format: YYYYmmdd_HHMMSS_ffffff)
    event_timestamp: Optional[str] = Form(None),
    # Optional client-provided content hash for idempotency/verification
    content_sha256: Optional[str] = Form(None),
    run_id: Optional[str] = Form(None),
    # 本体
    weights: UploadFile = File(...)
):
    """
    エッジから集約済み重み(.pt など)を受け取り、グローバルモデルを更新。
    - 受けたものを検証→保存→次ラウンド配布を準備→新メタを返す
    """
    # 1) 入力検証（軽）
    t_entry = time.time()
    federation_run = _normalize_federation_run(run_id)
    central_time_logger.log_event(
        "edge_update_entry",
        {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "run_id": run_id, "federation_run_id": federation_run},
    )
    if int(num_clients) <= 0 or int(sum_n_samples) <= 0:
        raise HTTPException(422, detail="num_clients と sum_n_samples は正の整数である必要があります")

    # Stream upload to disk while computing SHA to avoid holding large files in memory
    CHUNK = 16 * 1024
    ts_stream = time.time()
    recv_dir = (BASE_DIR / f"r{round}" / edge_id)
    recv_dir.mkdir(parents=True, exist_ok=True)
    base_filename = f"agg_{time.strftime('%Y%m%d_%H%M%S')}"
    raw_path = recv_dir / f"{base_filename}.pt"
    hasher = hashlib.sha256()
    total_bytes = 0
    try:
        # Use aiofiles to avoid blocking the event loop during large file writes
        async with aiofiles.open(raw_path, 'wb') as fh:
            while True:
                chunk = await weights.read(CHUNK)
                if not chunk:
                    break
                await fh.write(chunk)
                hasher.update(chunk)
                total_bytes += len(chunk)
    except Exception as e:
        logger.exception(f"ディスクへのストリームアップロードに失敗しました: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "code": "write_failed", "detail": str(e)})

    if total_bytes < 64:
        # cleanup small file
        try:
            raw_path.unlink(missing_ok=True)
        except Exception:
            pass
        return JSONResponse(status_code=400, content={"status": "error", "code": "empty_payload", "detail": "重みペイロードが空または小さすぎます"})

    actual_sha = "sha256:" + hasher.hexdigest()
    central_time_logger.log_event("upload_streamed", {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "bytes": total_bytes, "sha256": actual_sha, "duration_s": time.time() - ts_stream})
    # if client provided a hash, verify
    if content_sha256:
        # normalize forms like optional prefix
        if content_sha256.strip() != actual_sha:
            return JSONResponse(status_code=422, content={"status": "error", "code": "hash_mismatch", "detail": "アップロード内容と content_sha256 が一致しません", "expected": actual_sha, "received": content_sha256})

    # duplicate idempotency check
    # First-wins per edge+round: if we've already accepted any update from this edge for this round,
    # ignore subsequent uploads (this complements content-sha idempotency).
    try:
        round_key = str(int(round))
    except Exception:
        round_key = str(round)

    # Prefer marker backend (redis or sqlite) for fast atomic checks; fall back to in-memory JSON stores
    try:
        stored = marker_backend.get_edge_round_sha(round, edge_id, run_id)
        if stored is not None:
            if stored == actual_sha:
                ack_ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                central_time_logger.log_event(
                    "duplicate_ignored_per_edge",
                    {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "sha256": actual_sha, "federation_run_id": federation_run},
                )
                central_time_logger.log_event("duplicate_check_done", {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "duplicate": True, "elapsed_s": time.time() - t_entry})
                return JSONResponse(status_code=200, content={"status": "duplicate_ignored", "ack": True, "ack_timestamp": ack_ts, "sha256": actual_sha})
            else:
                # Different sha for same (federation_run, round, edge): legitimate re-aggregation or mixed payload
                logger.warning(
                    f"マーカー競合 edge={edge_id} round={round} federation_run={federation_run}: "
                    f"保存済みSHA={stored} 受信SHA={actual_sha}。新ペイロードを採用してマーカーを更新します"
                )
    except Exception:
        # fallback to in-memory map (legacy behaviour)
        edge_key = f"{federation_run}::{edge_id}"
        edge_map = _received_by_edge.get(round_key, {})
        if edge_key in edge_map:
            if edge_map[edge_key] == actual_sha:
                ack_ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                central_time_logger.log_event("duplicate_ignored_per_edge", {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "sha256": actual_sha})
                central_time_logger.log_event("duplicate_check_done", {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "duplicate": True, "elapsed_s": time.time() - t_entry})
                return JSONResponse(status_code=200, content={"status": "duplicate_ignored", "ack": True, "ack_timestamp": ack_ts, "sha256": actual_sha})
            else:
                logger.warning(
                    f"メモリ上マーカー競合 edge={edge_id} round={round} federation_run={federation_run}: "
                    f"保存済みSHA={edge_map[edge_key]} 受信SHA={actual_sha}。新ペイロードを採用してマーカーを更新します"
                )

    try:
        if marker_backend.has_received_hash(actual_sha):
            ack_ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            central_time_logger.log_event("duplicate_ignored", {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "sha256": actual_sha})
            central_time_logger.log_event("duplicate_check_done", {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "duplicate": True, "elapsed_s": time.time() - t_entry})
            return JSONResponse(status_code=200, content={"status": "duplicate_ignored", "ack": True, "ack_timestamp": ack_ts, "sha256": actual_sha})
    except Exception:
        async with _hash_lock:
            exists = actual_sha in _received_hashes
        if exists:
            ack_ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            central_time_logger.log_event("duplicate_ignored", {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "sha256": actual_sha})
            central_time_logger.log_event("duplicate_check_done", {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "duplicate": True, "elapsed_s": time.time() - t_entry})
            return JSONResponse(status_code=200, content={"status": "duplicate_ignored", "ack": True, "ack_timestamp": ack_ts, "sha256": actual_sha})

    # persist per-edge-per-round first-wins marker
    try:
        # persist to configured marker backend (sqlite or redis)
        marker_backend.set_edge_round_sha(round, edge_id, actual_sha, run_id)
        central_time_logger.log_event(
            "received_by_edge_persisted",
            {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "sha256": actual_sha, "federation_run_id": federation_run},
        )
        logger.info(f"received_by_edge を永続化しました edge={edge_id} round={round} sha={actual_sha}")
        central_time_logger.log_event("duplicate_check_done", {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "duplicate": False, "elapsed_s": time.time() - t_entry})
    except Exception:
        # best-effort fallback to JSON persistence
        try:
            _received_by_edge.setdefault(round_key, {})[f"{federation_run}::{edge_id}"] = actual_sha
            _save_received_by_edge(_received_by_edge)
            central_time_logger.log_event(
                "received_by_edge_persisted",
                {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "sha256": actual_sha, "federation_run_id": federation_run},
            )
        except Exception:
            logger.exception("received_by_edge の永続化に失敗しました。処理を継続します")

    # Persist received-hash early to avoid duplicate processing on retries.
    # This ensures other servers/clients won't re-send the same payload
    # while we perform heavier processing (load, validate, aggregate).
    try:
        marker_backend.add_received_hash(actual_sha)
        central_time_logger.log_event("received_persisted", {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "sha256": actual_sha})
        logger.info(f"受信ハッシュを早期永続化しました edge={edge_id}, round={round}, sha={actual_sha}")
    except Exception:
        # fallback to JSON
        try:
            async with _hash_lock:
                _received_hashes.add(actual_sha)
                _save_received_hashes(_received_hashes)
            central_time_logger.log_event("received_persisted", {"edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id, "round": round, "sha256": actual_sha})
            logger.info(f"受信ハッシュを早期永続化しました edge={edge_id}, round={round}, sha={actual_sha}")
        except Exception:
            logger.exception("受信ハッシュの早期永続化に失敗しました。ブロックせず継続します")

    # 2) 一時保存はすでにストリームで行っている。ここでは保存済みファイルを参照して読み込む
    # raw_path は上で作成されているはず
    try:
        raw_path  # ensure variable present
    except NameError:
        # fallback: create recv_dir and raw_path if streaming didn't run
        ts = time.strftime("%Y%m%d_%H%M%S")
        recv_dir = (BASE_DIR / f"r{round}" / edge_id)
        recv_dir.mkdir(parents=True, exist_ok=True)
        base_filename = f"agg_{ts}"
        raw_path = recv_dir / f"{base_filename}.pt"

    ts = time.strftime("%Y%m%d_%H%M%S")
    base_filename = raw_path.stem
    meta_path = raw_path.with_suffix('.meta.json')
    received_meta = {
        "received_at": ts,
        "edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id,
        "round": int(round),
        "num_clients": int(num_clients),
        "sum_n_samples": int(sum_n_samples),
        "model_id": model_id,
        "base_hash": base_hash,
        "original_filename": weights.filename,
        "content_sha256": actual_sha,
        "run_id": run_id,
        "federation_run_id": federation_run,
        "event_timestamp": event_timestamp,
    }
    try:
        meta_path.write_text(json.dumps(received_meta, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.exception("受信ペイロード用メタファイルの書き込みに失敗しました")

    try:
        # Load from file to avoid extra memory copy. Offload to thread to avoid blocking the event loop.
        # weights_only=True は必須。legacy format (.tar) や weights_only 非対応ファイルは
        # セキュリティリスク（RCE）があるため受け付けない。
        try:
            state_dict = await asyncio.to_thread(torch.load, str(raw_path), map_location="cpu", weights_only=True)
        except TypeError:
            # 古い PyTorch バージョンが weights_only kwarg を受け付けない場合
            # weights_only なしでは任意コード実行が可能なため、ここでも拒否する
            logger.error("torch.load が weights_only 引数をサポートしていません。対応版の PyTorch にアップグレードしてください")
            raise HTTPException(400, detail="サーバは weights_only 対応の PyTorch が必要です。レガシー形式は受け付けません")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"集約重みの読み込みに失敗しました {raw_path}: {e}")
        raise HTTPException(400, detail=f"集約重みの読み込みに失敗しました: {e}")

    check_state_dict_keys(state_dict)

    central_time_logger.log_event("model_received", {
        "edge_id": edge_id, "terminal_id": terminal_id,
            "terminal_id": terminal_id,
        "round": round,
        "num_clients": num_clients,
        "sum_n_samples": sum_n_samples,
        "event_timestamp": event_timestamp or ts,
        "run_id": run_id,
        "sha256": actual_sha,
    })

    async with round_updates_lock:
        if round not in round_updates:
            round_updates[round] = {}
        # このエッジからの更新を保存（同じラウンドで同じエッジから来たら上書き）
        round_updates[round][edge_id] = {
            "state_dict": state_dict,
            "num_clients": int(num_clients),
            "sum_n_samples": int(sum_n_samples),
        }

        # 現在のラウンドに集まっている更新の数を取得
        num_collected_edges = len(round_updates[round])

        # しきい値に達しているかチェック
        should_aggregate = num_collected_edges >= CENTRAL_AGGREGATION_THRESHOLD

        if should_aggregate:
            # mark aggregation started and take snapshot of updates then clear in-memory bucket
            central_time_logger.log_event("aggregation_started", {
                "round": round,
                "collected_edges": num_collected_edges
            })
            # atomically pop the updates for this round so subsequent incoming updates start fresh
            current_round_all_updates = round_updates.pop(round, {})
            # schedule background aggregation and return immediately so HTTP request is not blocked
            try:
                asyncio.create_task(_background_aggregate(round, current_round_all_updates, request))
            except Exception:
                logger.exception("バックグラウンド集約タスクのスケジュールに失敗しました")
            return JSONResponse({
                "status": "accepted_and_queued",
                "message": f"エッジ {edge_id} のラウンド {round} の更新を受理し、集約をキューに入れました。",
                "collected_edges": num_collected_edges,
                "aggregation_threshold": CENTRAL_AGGREGATION_THRESHOLD,
            })
        else:
            # しきい値に達していない場合、集約は行わない
            return JSONResponse({
                "status": "accepted_and_waiting",
                "message": f"エッジ {edge_id} のラウンド {round} の更新を受理しました。他エッジの更新を待機中です。",
                "collected_edges": num_collected_edges,
                "aggregation_threshold": CENTRAL_AGGREGATION_THRESHOLD,
            })


# -----------------------------------------------------------------------
# メトリクス SQLite DB
# -----------------------------------------------------------------------
METRICS_DB_PATH = BASE_DIR / "training_metrics.db"


def init_metrics_db() -> None:
    """metrics テーブルを作成する（既存なら何もしない）。既存DBのカラム不足も補完する。"""
    METRICS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(METRICS_DB_PATH), timeout=30) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                edge_id          TEXT    NOT NULL,
                round            INTEGER NOT NULL,
                accuracy         REAL,
                loss             REAL,
                data_id          TEXT    NOT NULL,
                event_timestamp  TEXT,
                app_type         TEXT,
                app_index        INTEGER,
                satisfaction_before REAL,
                satisfaction_after  REAL
            )
        """)
        conn.commit()
        # 既存DBに新カラムが不足している場合は自動追加（マイグレーション）
        try:
            cur = conn.execute("PRAGMA table_info(metrics)")
            existing_cols = {row[1] for row in cur.fetchall()}
            migrations = [
                ("app_type",            "ALTER TABLE metrics ADD COLUMN app_type TEXT"),
                ("app_index",           "ALTER TABLE metrics ADD COLUMN app_index INTEGER"),
                ("satisfaction_before", "ALTER TABLE metrics ADD COLUMN satisfaction_before REAL"),
                ("satisfaction_after",  "ALTER TABLE metrics ADD COLUMN satisfaction_after REAL"),
            ]
            for col, sql in migrations:
                if col not in existing_cols:
                    try:
                        conn.execute(sql)
                        conn.commit()
                    except Exception:
                        pass
        except Exception:
            pass


# 起動時に必ず初期化
try:
    init_metrics_db()
except Exception:
    logger.exception("メトリクス DB の初期化に失敗しました")


# Function to validate data consistency using unique IDs
def validate_data_id(data_id, round):
    """
    Validate the data ID to ensure consistency.
    """
    # Example: Check if the data ID has already been processed for the given round
    processed_ids_file = BASE_DIR / "processed_data_ids.json"
    if processed_ids_file.exists():
        with open(processed_ids_file, 'r', encoding='utf-8') as f:
            processed_ids = json.load(f)
    else:
        processed_ids = {}

    if str(round) in processed_ids and data_id in processed_ids[str(round)]:
        raise HTTPException(400, detail=f"ラウンド {round} でデータ ID が重複しています: {data_id}")

    # Mark the data ID as processed
    processed_ids.setdefault(str(round), []).append(data_id)
    with open(processed_ids_file, 'w', encoding='utf-8') as f:
        json.dump(processed_ids, f, ensure_ascii=False, indent=2)

@router.post("/upload_training_metrics")
async def upload_training_metrics(
    request: Request,
    edge_id: str = Form(...),
    terminal_id: Optional[str] = Form(None),
    round: int = Form(...),
    accuracy: Optional[float] = Form(None),
    loss: Optional[float] = Form(None),
    data_id: str = Form(...),
    event_timestamp: Optional[str] = Form(None),
    app_type: Optional[str] = Form(None),
    app_index: Optional[int] = Form(None),
    satisfaction_before: Optional[float] = Form(None),
    satisfaction_after: Optional[float] = Form(None),
    tp_measured_mbps: Optional[float] = Form(None),
    rtt_measured_ms: Optional[float] = Form(None),
):
    """
    エッジデバイスから学習メトリクスを受け取り SQLite に保存する。
    app_type / satisfaction_before / satisfaction_after はアプリ種別・満足度分析用。
    """
    try:
        validate_data_id(data_id, round)

        ts = event_timestamp or datetime.now().isoformat()

        central_time_logger.log_event("training_metrics_received", {
            "edge_id": edge_id,
            "terminal_id": terminal_id,
            "round": round,
            "accuracy": accuracy,
            "loss": loss,
            "app_type": app_type,
            "satisfaction_before": satisfaction_before,
            "satisfaction_after": satisfaction_after,
            "tp_measured_mbps": tp_measured_mbps,
            "rtt_measured_ms": rtt_measured_ms,
            "data_id": data_id,
            "event_timestamp": ts,
        })

        await asyncio.to_thread(
            _insert_metrics,
            edge_id, round, accuracy, loss, data_id, ts, terminal_id,
            app_type, app_index, satisfaction_before, satisfaction_after, tp_measured_mbps, rtt_measured_ms,
        )

        # [AI] リアルタイム実況 + 異常検知（ノンブロッキング）
        try:
            from central_server.ai_advisor import get_advisor
            advisor = get_advisor()
            if advisor:
                asyncio.create_task(advisor.on_metrics_received({
                    "edge_id": edge_id,
                    "terminal_id": terminal_id,
                    "round": round,
                    "accuracy": accuracy,
                    "loss": loss,
                    "app_type": app_type,
                    "app_index": app_index,
                    "satisfaction_before": satisfaction_before,
                    "satisfaction_after": satisfaction_after,
            "tp_measured_mbps": tp_measured_mbps,
            "rtt_measured_ms": rtt_measured_ms,
                }))
        except Exception:
            pass

        return JSONResponse({"status": "success", "message": "メトリクスを受信し保存しました。"})

    except HTTPException as e:
        raise e
    except Exception as e:
        logger.exception("学習メトリクスのアップロード処理に失敗しました。")
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


def _insert_metrics(
    edge_id: str, round: int, accuracy: Any, loss: Any, data_id: str, event_timestamp: str, terminal_id: Optional[str],
    app_type: Any, app_index: Any, satisfaction_before: Any, satisfaction_after: Any,
    tp_measured_mbps: Any = None, rtt_measured_ms: Any = None
) -> None:
    """Insert training metrics into the SQLite DB."""
    with sqlite3.connect(str(METRICS_DB_PATH), timeout=30) as conn:
        conn.execute(
            """INSERT INTO metrics
               (edge_id, round, accuracy, loss, data_id, event_timestamp,
                app_type, app_index, satisfaction_before, satisfaction_after)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (edge_id, int(round), accuracy, loss, data_id, event_timestamp,
             app_type, app_index, satisfaction_before, satisfaction_after),
        )
        conn.commit()
    logger.debug(f"_insert_metrics: edge={edge_id} round={round} data_id={data_id} app_type={app_type}")

# Save received data in a structured format for analysis
def save_received_data_for_analysis(meta, state_dict):
    analysis_dir = BASE_DIR / "analysis_data"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    
    # Save metadata
    meta_path = analysis_dir / f"meta_round_{meta['round']}_edge_{meta['edge_id']}.json"
    with open(meta_path, 'w', encoding='utf-8') as meta_file:
        json.dump(meta, meta_file, ensure_ascii=False, indent=2)

    # Save state_dict
    state_dict_path = analysis_dir / f"state_dict_round_{meta['round']}_edge_{meta['edge_id']}.pt"
    torch.save(state_dict, state_dict_path)
