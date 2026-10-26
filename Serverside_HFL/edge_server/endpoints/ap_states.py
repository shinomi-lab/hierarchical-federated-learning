"""
GET  /api/v1/ap_states          — AP の状態（模擬値・接続数・実測）。全エッジの端末の合計は中央サーバが持つ。
POST /api/v1/ap_states/connect  — 端末が AP に繋がった（切り替えた）ことの通知。
POST /api/v1/ap_states/observe  — 端末が計測のたびに送る、実際に繋がっている AP と実測の TP・RTT。

OpenSpec record-connected-ap: 端末は自分のエッジにしか話さないので、エッジは受けた通知・観測を中央サーバへ転送し、
GET は中央の集計を返す。中央に届かない・遅いときは、このエッジが受けた分だけで計算した代替を返す
（各 AP の ap_states_source と X-Ap-States-Source ヘッダが "local"）。集計は shared.ap_state。
最上位のキーは AP だけにする（旧 APK のパーサが最上位の全キーを AP オブジェクトとして読む）。
サーバの Python は 3.9（`str | None` など 3.10 以降の構文を使わない）。
"""
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from edge_server import config
from shared.ap_state import ApStateAggregator, ap_ids_for

router = APIRouter(prefix="/api/v1", tags=["ap_states"])
logger = logging.getLogger("edge.ap_states")

CENTRAL_TIMEOUT_S = 2.0  # 中央が遅くても端末の計測・Greedy の判断を長く止めない

# 代替用（中央に届かないとき）。このエッジが受けた分だけを持つ。
local: ApStateAggregator = ApStateAggregator(ap_ids=ap_ids_for(config.AP_NUM_MAX))


class ApConnectRequest(BaseModel):
    terminal_id: str
    from_ap: Optional[str] = None  # 互換のために受け取るが、数え方には使わない
    to_ap: str


class ApObserveRequest(BaseModel):
    terminal_id: str
    ap: str
    tp_mbps: Optional[float] = None
    rtt_ms: Optional[float] = None


def _http_request(method: str, url: str, **kw):
    return httpx.request(method, url, timeout=CENTRAL_TIMEOUT_S, **kw)


def _central(method: str, path: str, **kw):
    """中央へ要求する。失敗・非 2xx は None（呼び出し側が代替に回る）。"""
    url = config.CENTRAL_SERVER_URL.rstrip("/") + path
    try:
        r = _http_request(method, url, **kw)
        if 200 <= r.status_code < 300:
            return r
        if r.status_code == 400:  # 中央が拒否: エッジと中央の AP_NUM_MAX が食い違っている可能性（届かないのとは別）
            logger.warning("ap_states: 中央が %s %s を拒否した (400)。エッジと中央の AP_NUM_MAX の食い違いの可能性", method, path)
        return None
    except Exception as e:  # 接続不可・タイムアウトなど
        logger.warning("ap_states: 中央サーバに届かない (%s %s): %s", method, path, e)
        return None


def _check_ap(ap: str) -> None:
    if not local.is_valid_ap(ap):
        raise HTTPException(status_code=400, detail="Unknown AP: %s" % ap)


@router.get("/ap_states")
def get_ap_states():
    r = _central("GET", "/api/v1/ap_states")
    if r is not None:
        return JSONResponse(content=r.json(), headers={"X-Ap-States-Source": "central"})
    logger.warning("ap_states: 中央の集計を使えないので、このエッジが受けた分だけで答える (ap_states_source=local)")
    body = {ap: dict(s, ap_states_source="local") for ap, s in local.snapshot().items()}
    return JSONResponse(content=body, headers={"X-Ap-States-Source": "local"})


@router.post("/ap_states/connect")
def notify_ap_connect(req: ApConnectRequest):
    """端末の接続 AP を更新する（足し引きのカウンタではない）。from_ap は旧 APK との互換のために受け取るだけ。"""
    _check_ap(req.to_ap)
    local.connect(req.terminal_id, req.to_ap)
    r = _central("POST", "/api/v1/ap_states/connect", json=req.dict())
    connected = r.json().get("connected") if r is not None else None
    if connected is None:
        connected = {ap: s["n_connected"] for ap, s in local.snapshot().items()}
    return {"ok": True, "connected": connected}


@router.post("/ap_states/observe")
def observe_ap(req: ApObserveRequest):
    _check_ap(req.ap)
    local.observe(req.terminal_id, req.ap, req.tp_mbps, req.rtt_ms)
    _central("POST", "/api/v1/ap_states/observe", json=req.dict())  # 失敗しても端末の計測は止めない
    return {"ok": True}
