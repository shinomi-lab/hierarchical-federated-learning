# edge_server/endpoints/aggregation_debug.py
from __future__ import annotations

import io, os, uuid, asyncio, logging, json, time
from pathlib import Path
from typing import List, Tuple, Dict, Optional, Iterable

import torch
import httpx
from fastapi import APIRouter, HTTPException, Body

from edge_server.state import terminal_state_dicts, state_dict_lock, UpdateRecord, clear_round
from edge_server.config import CENTRAL_SERVER_URL, EDGE_SERVER_ID, AGGREGATION_CACHE_DIR

router = APIRouter()
EDGE_UPDATE_PATH = "/edge_update"
SEND_RETRIES = 3
SEND_RETRY_DELAY_SEC = 5
AGGREGATION_CACHE_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("aggregation_debug")
logger.setLevel(logging.DEBUG)

_pending_index: Dict[str, dict] = {}
_pending_index_lock = asyncio.Lock()


# ------------------ FedAvg helpers ------------------
def _check_same_keys_and_shapes(dicts: List[dict]) -> List[str]:
    logger.debug("[デバッグ] キー/形状の整合性を確認しています")
    if not dicts:
        raise ValueError("empty state_dicts")
    base_keys = set(dicts[0].keys())
    for i, sd in enumerate(dicts[1:], start=1):
        if set(sd.keys()) != base_keys:
            diff_plus = set(sd.keys()) - base_keys
            diff_minus = base_keys - set(sd.keys())
            raise ValueError(f"state_dict keys mismatch at #{i}: +{sorted(list(diff_plus))}, -{sorted(list(diff_minus))}")
    for k in base_keys:
        shape0 = tuple(dicts[0][k].size()) if hasattr(dicts[0][k], "size") else None
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


def _fedavg_state_dicts_weighted(items: List[Tuple[dict, int]]) -> dict:
    logger.debug(f"[デバッグ] FedAvg 実行中 件数={len(items)}")
    if not items:
        raise ValueError("no items to aggregate")
    sds = [sd for sd, _ in items]
    keys = _check_same_keys_and_shapes(sds)
    weights = _sanitize_weights(n for _, n in items)
    out: Dict[str, torch.Tensor] = {}
    for k in keys:
        first = sds[0][k]
        if torch.is_floating_point(first):
            acc = None
            for i, (sd, _) in enumerate(items):
                t = sd[k].detach().to("cpu").float()
                if torch.isnan(t).any() or torch.isinf(t).any():
                    raise ValueError(f"NaN/Inf detected at key='{k}' in update #{i}")
                w = weights[i]
                acc = t.mul_(w) if acc is None else acc.add_(t, alpha=w)
            out[k] = acc
        else:
            out[k] = first.detach().to("cpu")
    logger.debug("[デバッグ] FedAvg 完了")
    return out


# ------------------ Pending helpers ------------------
def _meta_path_for(filename: str) -> Path:
    return AGGREGATION_CACHE_DIR / f"{filename}.meta.json"


def _entry_path_for(filename: str) -> Path:
    return AGGREGATION_CACHE_DIR / filename


def _load_pending_index_from_disk():
    logger.debug("[デバッグ] ディスクから保留インデックスを読み込み中")
    for p in AGGREGATION_CACHE_DIR.glob("*.meta.json"):
        try:
            dd = json.loads(p.read_text(encoding="utf-8"))
            fname = dd.get("filename") or p.stem.replace(".meta", "")
            _pending_index[fname] = dd
        except Exception:
            logger.exception(f"保留メタの読み込みに失敗しました {p}")


def _register_pending(filename: str, meta: dict):
    meta["filename"] = filename
    meta_path = _meta_path_for(filename)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _pending_index[filename] = meta
    logger.debug(f"[デバッグ] 保留を登録しました {filename}")


def _unregister_pending(filename: str):
    try:
        meta_path = _meta_path_for(filename)
        if meta_path.exists():
            meta_path.unlink()
    except Exception as e:
        logger.warning(f"メタ削除に失敗しました {filename}: {e}")
    _pending_index.pop(filename, None)
    logger.debug(f"[デバッグ] 保留の登録を解除しました {filename}")


_load_pending_index_from_disk()


# ------------------ Snapshot helpers ------------------
async def _snapshot_round(round_id: int) -> Tuple[List[Tuple[dict, int]], int, int]:
    logger.debug(f"[デバッグ] ラウンド {round_id} のスナップショット取得中")
    async with state_dict_lock:
        bucket: List[UpdateRecord] = terminal_state_dicts.get(round_id, [])
        if not bucket:
            raise HTTPException(status_code=400, detail=f"ラウンド {round_id} に集約する更新がありません")
        items = [(rec.state_dict, int(rec.n_samples)) for rec in bucket]
        num_clients = len(bucket)
        sum_n_samples = sum(int(rec.n_samples) for rec in bucket)
    logger.debug(f"[デバッグ] スナップショット取得完了: クライアント {num_clients} 件, サンプル合計 {sum_n_samples}")
    return items, num_clients, sum_n_samples


def _save_temp_aggregated(round_id: int, state_dict: dict, model_id: Optional[str]) -> str:
    # deterministic filename per round to avoid creating many files during rapid runs
    filename = f"agg_r{round_id}.pt"
    filepath = AGGREGATION_CACHE_DIR / filename
    tmp = filepath.with_suffix('.pt.tmp')
    try:
        torch.save(state_dict, tmp)
        tmp.replace(filepath)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

    # cleanup any previous uuid-suffixed files for this round
    try:
        for p in AGGREGATION_CACHE_DIR.glob(f"agg_r{round_id}_*.pt"):
            if p.name != filename:
                try:
                    p.unlink()
                except Exception:
                    logger.debug(f"[デバッグ] 古い集約ファイルを削除できませんでした {p}")
                try:
                    mp = _meta_path_for(p.name)
                    if mp.exists():
                        mp.unlink()
                except Exception:
                    pass
    except Exception:
        logger.debug("[デバッグ] 古い集約ファイルのクリーンアップ対象なし、またはクリーンアップ失敗")

    meta = {
        "round": int(round_id),
        "model_id": model_id,
        "edge_id": EDGE_SERVER_ID,
        "num_clients": None,
        "sum_n_samples": None,
        "saved_at": int(time.time()),
    }
    _register_pending(filename, meta)
    logger.debug(f"[デバッグ] 一時集約ファイルを保存しました {filename}")
    return filename


# ------------------ Central send ------------------
async def _post_to_central_from_path(file_path: Path, *, edge_id: str, round_id: int,
                                     model_id: Optional[str], num_clients: int, sum_n_samples: int):
    url = f"{CENTRAL_SERVER_URL.rstrip('/')}{EDGE_UPDATE_PATH}"
    data = {
        "edge_id": edge_id,
        "round": str(round_id),
        "num_clients": str(num_clients),
        "sum_n_samples": str(sum_n_samples),
    }
    if model_id:
        data["model_id"] = model_id
    last_exc = None
    async with httpx.AsyncClient(timeout=120.0) as client:
        for attempt in range(SEND_RETRIES):
            try:
                logger.debug(f"[デバッグ] 中央へ送信中 {file_path.name} 試行 {attempt+1}")
                with open(file_path, "rb") as f:
                    files = {"weights": (file_path.name, f, "application/octet-stream")}
                    resp = await client.post(url, files=files, data=data)
                    resp.raise_for_status()
                logger.debug(f"[デバッグ] 送信成功 {file_path.name} status={resp.status_code}")
                try:
                    return resp.json()
                except Exception:
                    return {"status_code": resp.status_code}
            except Exception as e:
                last_exc = e
                logger.warning(f"[デバッグ] 送信試行 {attempt+1} が失敗しました: {e}")
                if attempt < SEND_RETRIES - 1:
                    await asyncio.sleep(SEND_RETRY_DELAY_SEC)
    raise HTTPException(status_code=502, detail=f"中央へのプッシュに失敗しました: {last_exc}")


# ------------------ Interactive ------------------
async def _ask_user_confirmation(prompt: str) -> bool:
    return True

async def _interactive_confirm_and_send(filename: str, meta: dict) -> dict:
    ok = await _ask_user_confirmation(
        f"集約ファイル '{filename}'（ラウンド {meta.get('round')}）を中央へ送信しますか？ (y/n): "
    )
    if not ok:
        logger.debug(f"[デバッグ] オペレータが {filename} の送信を拒否しました")
        return {"status": "declined_by_operator", "filename": filename}
    path = AGGREGATION_CACHE_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="保留ファイルが見つかりません")
    central_resp = await _post_to_central_from_path(path, edge_id=EDGE_SERVER_ID,
                                                    round_id=int(meta.get("round", -1)),
                                                    model_id=meta.get("model_id"),
                                                    num_clients=int(meta.get("num_clients") or 0),
                                                    sum_n_samples=int(meta.get("sum_n_samples") or 0))
    async with _pending_index_lock:
        _unregister_pending(filename)
    try:
        async with state_dict_lock:
            clear_round(int(meta.get("round")))
    except Exception:
        logger.warning("[デバッグ] インタラクティブ送信後にラウンドをクリアできませんでした")
    try:
        path.unlink()
    except Exception as e:
        logger.warning(f"[デバッグ] 一時ファイル削除に失敗しました {path}: {e}")
    return {"status": "sent", "filename": filename, "central_response": central_resp}


# ------------------ Public endpoint ------------------
@router.post("/aggregate_and_push", summary="Aggregate and optionally send to central")
async def aggregate_and_push(
    round_id: int = Body(..., embed=True),
    model_id: Optional[str] = Body(None, embed=True),
    auto_send: bool = Body(False, embed=True),
    interactive: bool = Body(False, embed=True)
):
    logger.debug(f"[デバッグ] aggregate_and_push 開始: round={round_id}, auto_send={auto_send}, interactive={interactive}")
    items, num_clients, sum_n_samples = await _snapshot_round(round_id)
    try:
        avg_sd = _fedavg_state_dicts_weighted(items)
    except Exception as e:
        logger.exception("[デバッグ] 集約に失敗しました")
        raise HTTPException(status_code=500, detail=f"集約に失敗しました: {e}")
    filename = _save_temp_aggregated(round_id, avg_sd, model_id)
    async with _pending_index_lock:
        meta = _pending_index.get(filename, {})
        meta["num_clients"] = num_clients
        meta["sum_n_samples"] = sum_n_samples
        meta["round"] = int(round_id)
        _register_pending(filename, meta)
    logger.debug(f"[デバッグ] 集約を保存し保留として登録しました: {filename}")

    if auto_send:
        logger.debug(f"[デバッグ] 自動送信中 {filename}")
        path = AGGREGATION_CACHE_DIR / filename
        try:
            central_resp = await _post_to_central_from_path(path, edge_id=EDGE_SERVER_ID,
                                                            round_id=round_id,
                                                            model_id=model_id,
                                                            num_clients=num_clients,
                                                            sum_n_samples=sum_n_samples)
            async with _pending_index_lock:
                _unregister_pending(filename)
            async with state_dict_lock:
                clear_round(round_id)
            try:
                path.unlink()
            except Exception:
                pass
            return {"status": "sent", "filename": filename, "central_response": central_resp}
        except Exception as e:
            logger.exception("[デバッグ] 自動送信に失敗しました")
            return {"status": "failed_send", "error": str(e), "filename": filename}

    if interactive:
        return await _interactive_confirm_and_send(filename, meta)

    return {"status": "pending", "filename": filename, "num_clients": num_clients, "sum_n_samples": sum_n_samples}
