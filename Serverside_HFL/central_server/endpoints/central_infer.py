# Serverside_HFL/central_server/endpoints/central_infer.py
"""中央集権モード（centralized-mode）の推論エンドポイント。端末はアプリ種別だけを送り、中央が事前学習したモデルで推奨 AP を返す。
入力は「アプリの要求値（z-score）＋one-hot」で、端末の実測 TP/RTT は使わない。"""
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from central_server.config import ROOT_DIR, STATE_DIR
from shared.central_model import forward_scores, state_sha256
from shared.inference_inputs import inference_input, load_norm_stats

router = APIRouter(tags=["central_infer"])

MODEL_PATH: Path = STATE_DIR / "central_model.pt"
META_PATH: Path = STATE_DIR / "central_model.meta.json"
APPS_PATH: Path = ROOT_DIR / "app.json"
NORM_STATS_PATH: Path = STATE_DIR / "norm_stats.json"
LOG_PATH: Path = STATE_DIR / "central_infer.jsonl"

_cache: dict = {"key": None, "sd": None, "sha": None, "norm": None}


class InferRequest(BaseModel):
    terminal_id: str
    app_type: str


def reset_cache() -> None:
    _cache.update(key=None, sd=None, sha=None, norm=None)


def load_model() -> bool:
    """中央モデルを読み込む（起動時とリクエスト時に呼ぶ。ファイルが更新されていれば読み直す）。無ければ False（例外にしない）。"""
    try:
        key = (MODEL_PATH.stat().st_mtime_ns, META_PATH.stat().st_mtime_ns if META_PATH.is_file() else None)
    except OSError:
        reset_cache()
        return False
    if _cache["key"] == key:
        return True
    try:
        import torch
        sd = torch.load(str(MODEL_PATH), map_location="cpu")
        norm = None
        if META_PATH.is_file():
            # 正規化は中央モデルを学習したときの統計を使う（教師データを作り直すと norm_stats.json と食い違うため）
            norm = (json.loads(META_PATH.read_text(encoding="utf-8")) or {}).get("norm")
        if not norm:
            norm = load_norm_stats(NORM_STATS_PATH)
        _cache.update(key=key, sd=sd, sha=state_sha256(sd), norm=norm)
        return True
    except Exception:
        reset_cache()
        return False


def _log(row: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _respond(status: int, body: dict, row: dict, t0: float) -> JSONResponse:
    row["status"] = status
    row["server_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    _log(row)
    return JSONResponse(status_code=status, content=body)


@router.post("/api/v1/central_infer")
def central_infer(req: InferRequest):
    t0 = time.perf_counter()
    row = {"ts": datetime.now().isoformat(), "terminal_id": req.terminal_id, "app_type": req.app_type,
           "chosen_ap": None, "model_sha256": None}
    if not load_model():
        return _respond(503, {"detail": f"central_model が用意されていません: {MODEL_PATH}"}, row, t0)
    try:
        apps = json.loads(APPS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return _respond(503, {"detail": f"app.json を読めません: {e}"}, row, t0)
    feats = inference_input({"app_type": req.app_type}, apps, _cache["norm"])
    if feats is None:
        return _respond(400, {"detail": f"未知のアプリ種別です: {req.app_type}"}, row, t0)
    scores = forward_scores(_cache["sd"], feats)
    chosen = scores.index(max(scores))
    row.update(chosen_ap=chosen, model_sha256=_cache["sha"])
    t_ms = round((time.perf_counter() - t0) * 1000, 3)
    row["status"], row["server_ms"] = 200, t_ms
    _log(row)
    return {"chosen_ap": chosen, "scores": scores, "model_sha256": _cache["sha"], "server_ms": t_ms}
