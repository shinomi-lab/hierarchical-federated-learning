# Serverside_HFL/central_server/endpoints/hfl_rec_infer.py
"""hfl_rec モードの推論エンドポイント。端末はアプリ種別だけを送り、中央の保存先にある最新のグローバルモデルで推奨 AP を返す。
入力は central_infer と同じ（アプリの要求値の z-score＋one-hot）で、端末の実測 TP/RTT は使わない。
保存先にモデルが無いときは初期モデルに戻らず 503 を返す。round <= 1（集約前）は is_initial=true を付けて返す。"""
import json
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from central_server.config import GLOBAL_MODEL_PATH, META_PATH, ROOT_DIR, STATE_DIR
from shared.central_model import forward_scores, state_sha256
from shared.inference_inputs import inference_input, load_norm_stats
from shared.model_source import snapshot_sources

router = APIRouter(tags=["hfl_rec_infer"])

APPS_PATH: Path = ROOT_DIR / "app.json"
NORM_STATS_PATH: Path = STATE_DIR / "norm_stats.json"
LOG_PATH: Path = STATE_DIR / "hfl_rec_infer.jsonl"

_cache: dict = {"key": None, "sd": None, "sha": None, "norm": None}


class InferRequest(BaseModel):
    terminal_id: str
    app_type: str


def reset_cache() -> None:
    _cache.update(key=None, sd=None, sha=None, norm=None)


def load_model() -> bool:
    """保存先のグローバルモデルを読み込む（更新時刻が変わっていれば読み直す）。無い・読めないなら False（例外にしない）。"""
    try:
        key = GLOBAL_MODEL_PATH.stat().st_mtime_ns
    except OSError:
        reset_cache()
        return False
    if _cache["key"] == key:
        return True
    try:
        import torch
        sd = torch.load(str(GLOBAL_MODEL_PATH), map_location="cpu")
        _cache.update(key=key, sd=sd, sha=state_sha256(sd), norm=load_norm_stats(NORM_STATS_PATH))
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


@router.post("/api/v1/hfl_rec_infer")
def hfl_rec_infer(req: InferRequest):
    t0 = time.perf_counter()
    row = {"ts": datetime.now().isoformat(), "terminal_id": req.terminal_id, "app_type": req.app_type,
           "chosen_ap": None, "model_sha256": None, "round": None, "is_initial": None}
    if not load_model():
        return _respond(503, {"detail": f"グローバルモデルがありません（初期モデルでは答えない）: {GLOBAL_MODEL_PATH}"}, row, t0)
    src = snapshot_sources(GLOBAL_MODEL_PATH, GLOBAL_MODEL_PATH, META_PATH)
    row.update(model_sha256=_cache["sha"], round=src["round"], is_initial=src["is_initial"])
    try:
        apps = json.loads(APPS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return _respond(503, {"detail": f"app.json を読めません: {e}"}, row, t0)
    feats = inference_input({"app_type": req.app_type}, apps, _cache["norm"])
    if feats is None:
        return _respond(400, {"detail": f"未知のアプリ種別です: {req.app_type}"}, row, t0)
    scores = forward_scores(_cache["sd"], feats)
    chosen = scores.index(max(scores))
    row["chosen_ap"] = chosen
    t_ms = round((time.perf_counter() - t0) * 1000, 3)
    row["status"], row["server_ms"] = 200, t_ms
    _log(row)
    return {"chosen_ap": chosen, "scores": scores, "model_sha256": _cache["sha"], "round": src["round"],
            "is_initial": src["is_initial"], "model_mtime": src["mtime"], "server_ms": t_ms}
