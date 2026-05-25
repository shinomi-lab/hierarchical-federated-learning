import os
import json
import logging
from pathlib import Path
from typing import Dict, List

import torch
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from shared.event_logger import get_event_logger

router = APIRouter()

# 保存ディレクトリ
ROOT = Path(__file__).resolve().parent.parent.parent
RECEIVED_EDGES_DIR = ROOT / "received_edges"
GLOBAL_MODELS_DIR = ROOT / "global_models"
RECEIVED_EDGES_DIR.mkdir(parents=True, exist_ok=True)
GLOBAL_MODELS_DIR.mkdir(parents=True, exist_ok=True)


def _load_state_dict_from_upload(f: UploadFile) -> Dict[str, torch.Tensor]:
    import io
    data = f.file.read()
    buf = io.BytesIO(data)
    return torch.load(buf, map_location="cpu", weights_only=True)


def _load_state_dict_bytes(data: bytes) -> Dict[str, torch.Tensor]:
    import io
    buf = io.BytesIO(data)
    return torch.load(buf, map_location="cpu", weights_only=True)


def _weighted_average_state_dicts(items: List[Dict[str, torch.Tensor]], weights: List[float]) -> Dict[str, torch.Tensor]:
    if not items:
        raise ValueError("no items provided")
    out: Dict[str, torch.Tensor] = {}
    total = float(sum(weights))
    if total <= 0:
        total = float(len(items))
        weights = [1.0 for _ in items]
    for key in items[0].keys():
        acc = None
        for sd, w in zip(items, weights):
            t = sd[key].to(torch.float32)
            contrib = t * float(w)
            acc = contrib if acc is None else acc + contrib
        out[key] = (acc / total).to(items[0][key].dtype)
    return out


@router.post("/receive_edge_weights")
async def receive_edge_weights(
    edge_id: str = Form(...),
    round: int = Form(...),
    num_clients: int = Form(...),
    sum_n_samples: int = Form(...),
    weights: UploadFile = File(...),
):
    """
    各エッジからの集約済み重みを中央で受領。ラウンドごとに (edge_id) 去重し、
    すべて揃ったらサンプル数重み付きで再集約してグローバルモデルを更新。
    """
    try:
        r = int(round)
    except Exception:
        raise HTTPException(status_code=400, detail="ラウンドが不正です")

    # 受領保存
    try:
        evt = get_event_logger("central_edge_aggregation", instance="central")
        evt.info("edge_weights_received", {"edge_id": edge_id, "round": int(round), "num_clients": int(num_clients), "sum_n_samples": int(sum_n_samples)})
    except Exception:
        evt = None
    rdir = RECEIVED_EDGES_DIR / f"r{r}" / edge_id
    rdir.mkdir(parents=True, exist_ok=True)
    import time, hashlib
    ts = time.strftime("%Y%m%d_%H%M%S")
    b = await weights.read()
    sha = "sha256:" + hashlib.sha256(b).hexdigest()
    wpath = rdir / f"weights_{ts}.pt"
    wpath.write_bytes(b)
    manifest = {
        "edge_id": edge_id,
        "round": r,
        "num_clients": int(num_clients),
        "sum_n_samples": int(sum_n_samples),
        "sha256": sha,
        "path": str(wpath),
        "saved_at": ts,
    }
    (rdir / f"manifest_{ts}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 設定の読み込み（期待台数・期待ID）
    expected_ids: List[str] = []
    expected_count: int = int(os.getenv("EXPECTED_EDGE_COUNT", "1"))
    try:
        cfg = (ROOT / "central_server" / "state" / "edges_expected.json")
        if cfg.exists():
            data = json.loads(cfg.read_text(encoding="utf-8"))
            expected_ids = list(data.get("expected_edge_ids", []))
            expected_count = int(data.get("expected_edge_count", expected_count))
    except Exception:
        logging.exception("failed to read edges_expected.json; falling back to env")
    if not expected_ids:
        # env EXPECTED_EDGE_IDS as comma-separated
        val = os.getenv("EXPECTED_EDGE_IDS")
        if val:
            expected_ids = [s.strip() for s in val.split(",") if s.strip()]

    # ラウンド内去重（最新ファイルのみ有効）: 同一 edge_id の古い受領は無視
    # 再集約のために現存エッジ提出を列挙
    round_dir = RECEIVED_EDGES_DIR / f"r{r}"
    sds: List[Dict[str, torch.Tensor]] = []
    ws: List[float] = []
    count_edges = 0
    unique_edges: List[str] = []
    for edge_folder in round_dir.iterdir():
        if not edge_folder.is_dir():
            continue
        # 最新 weights_*.pt を選ぶ
        candidates = sorted(edge_folder.glob("weights_*.pt"))
        if not candidates:
            continue
        latest = candidates[-1]
        try:
            sd = _load_state_dict_bytes(latest.read_bytes())
        except Exception:
            logging.exception(f"failed to load {latest}")
            continue
        # 重みは manifest の sum_n_samples を採用（無ければ1）
        ms = sorted(edge_folder.glob("manifest_*.json"))
        w = 1.0
        if ms:
            try:
                meta = json.loads(ms[-1].read_text(encoding="utf-8"))
                w = float(meta.get("sum_n_samples", 1.0))
                # edge_id を集計対象チェック
                eid = str(meta.get("edge_id", edge_folder.name))
                if expected_ids and eid not in expected_ids:
                    # 期待ID外はスキップ
                    continue
                unique_edges.append(eid)
            except Exception:
                pass
        sds.append(sd)
        ws.append(w)
        count_edges += 1

    # 期待数に満たない場合は待機応答
    received_unique = len(set(unique_edges)) if unique_edges else count_edges
    if received_unique < int(expected_count):
        if evt:
            try: evt.info("awaiting_edges", {"round": int(r), "received": int(received_unique), "expected": int(expected_count)});
            except Exception: pass
        return JSONResponse(status_code=200, content={
            "ack": True,
            "status": "queued",
            "edges_received": received_unique,
            "expected": int(expected_count)
        })

    try:
        agg = _weighted_average_state_dicts(sds, ws)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to re-aggregate: {e}")

    # グローバルモデル保存
    gpath = GLOBAL_MODELS_DIR / f"global_round_{r}.pt"
    torch.save(agg, str(gpath))

    # ラウンド集計JSONLを追記
    try:
        import time
        summary_dir = ROOT / "logs" / "rounds"
        summary_dir.mkdir(parents=True, exist_ok=True)
        idx = summary_dir / "round_summary.jsonl"
        entry = {
            "round": int(r),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "edges_received": int(received_unique),
            "expected_edges": int(expected_count),
            "global_model_path": str(gpath),
            "unique_edges": sorted(list(set(unique_edges))),
            "sum_weights": float(sum(ws)),
        }
        with open(idx, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if evt:
            try: evt.info("global_updated", {"round": int(r), "edges": int(received_unique), "model_path": str(gpath)});
            except Exception: pass
    except Exception:
        logging.exception("failed to write round summary jsonl")

    # メタ更新（round+1 に進め、model_id を更新）
    try:
        from central_server.endpoints.edge_update import save_meta
        meta = {
            "round": int(r) + 1,
            "model_id": f"global_round_{r}",
            "base_hash": "sha256:updated",
            "preferred_dtype": "torch_state_dict",
            "upload_endpoint": "/receive_terminal_weights/{terminal_id}",
            "aggregation_threshold": 1,
            "expected_param_len": 0,
            "order_version": 1,
        }
        save_meta(meta)
    except Exception:
        logging.exception("failed to update central META after re-aggregation")

    return JSONResponse(status_code=200, content={"ack": True, "status": "global_updated", "edges": count_edges, "round": r})
