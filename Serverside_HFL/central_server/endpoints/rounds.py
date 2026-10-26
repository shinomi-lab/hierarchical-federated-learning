from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import asyncio

import central_server.state as state

router = APIRouter(prefix="/rounds", tags=["rounds"])


class StartRoundPayload(BaseModel):
    round_id: int
    metadata: Optional[dict] = None


@router.post("/start")
async def start_round(payload: StartRoundPayload):
    # 現在はシンプルに round をグローバルモデルの round 値にセットする
    try:
        await state.set_global_model(int(payload.round_id), state.global_model_state.get("state_dict"))
        # clear any previous round updates
        await state.clear_round_updates(int(payload.round_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "round": payload.round_id}


@router.get("/status/{round_id}")
async def round_status(round_id: int):
    updates = await state.get_round_updates(int(round_id))
    return {"round": int(round_id), "n_updates": len(updates), "updates": updates}
