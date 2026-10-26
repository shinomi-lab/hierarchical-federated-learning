from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

import edge_server.state as state

router = APIRouter(prefix="/orchestration", tags=["orchestration"])


class StartTrainingPayload(BaseModel):
    round_id: int
    aggregation_threshold: Optional[int] = None
    metadata: Optional[dict] = None


@router.post("/start")
async def start_training(payload: StartTrainingPayload):
    # update local edge meta state
    try:
        new_meta = {"round": int(payload.round_id)}
        if payload.aggregation_threshold is not None:
            new_meta["aggregation_threshold"] = int(payload.aggregation_threshold)
        state.current_edge_state.update(new_meta)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "round": payload.round_id, "aggregation_threshold": state.current_edge_state.aggregation_threshold}


@router.get("/status")
async def orchestration_status():
    s = state.current_edge_state
    return {"round": s.round, "model_id": s.model_id, "aggregation_threshold": s.aggregation_threshold}
