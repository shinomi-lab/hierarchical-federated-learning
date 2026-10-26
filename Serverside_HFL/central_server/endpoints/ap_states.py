# Serverside_HFL/central_server/endpoints/ap_states.py
"""全エッジの端末の AP 観測を集計して返す（OpenSpec record-connected-ap）。

端末は自分のエッジにしか話さない。エッジは受けた観測・接続通知をここへ転送し、GET /ap_states はここの集計を返す。
集計は shared.ap_state（接続数＝端末ごとの最新の接続 AP、実測＝保持時間内の中央値、模擬値＝接続数からの M/M/1・Erlang-B）。
最上位のキーは AP だけ（旧 APK のパーサが最上位の全キーを AP オブジェクトとして読むため）。
"""
import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shared.ap_state import ApStateAggregator, ap_ids_for

router = APIRouter(prefix="/api/v1", tags=["ap_states"])

SOURCE = "central"


def _make_aggregator() -> ApStateAggregator:
    return ApStateAggregator(
        ap_ids=ap_ids_for(int(os.getenv("AP_NUM_MAX", "2"))),
        count_ttl_s=float(os.getenv("AP_STATE_TTL_S", "600")),
        measured_ttl_s=float(os.getenv("AP_MEASURED_TTL_S", "120")),
    )


aggregator: ApStateAggregator = _make_aggregator()


class ApConnectRequest(BaseModel):
    terminal_id: str
    from_ap: Optional[str] = None  # 互換のために受け取るが、数え方には使わない。`str | None` は 3.9 で読めない
    to_ap: str


class ApObserveRequest(BaseModel):
    terminal_id: str
    ap: str
    tp_mbps: Optional[float] = None
    rtt_ms: Optional[float] = None


def snapshot_response(agg: ApStateAggregator, source: str) -> JSONResponse:
    body = {ap: dict(state, ap_states_source=source) for ap, state in agg.snapshot().items()}
    return JSONResponse(content=body, headers={"X-Ap-States-Source": source})


@router.get("/ap_states")
async def get_ap_states():
    return snapshot_response(aggregator, SOURCE)


@router.post("/ap_states/connect")
async def notify_ap_connect(req: ApConnectRequest):
    try:
        aggregator.connect(req.terminal_id, req.to_ap)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown AP: %s" % req.to_ap)
    return {"ok": True, "connected": {ap: s["n_connected"] for ap, s in aggregator.snapshot().items()}}


@router.post("/ap_states/observe")
async def observe_ap(req: ApObserveRequest):
    try:
        aggregator.observe(req.terminal_id, req.ap, req.tp_mbps, req.rtt_ms)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown AP: %s" % req.ap)
    return {"ok": True}
