from __future__ import annotations

from fastapi import APIRouter, HTTPException

from edge_server.state import current_edge_state
from edge_server.endpoints.aggregation import aggregate_and_send_to_central_server
from edge_server.config import EDGE_SERVER_ID

router = APIRouter(prefix="/aggregation", tags=["aggregation"])


@router.post("/trigger_current")
async def trigger_current_aggregation(auto_send: bool = True):
    try:
        round_id = int(current_edge_state.round)
        await aggregate_and_send_to_central_server(EDGE_SERVER_ID, round_id, auto_send=auto_send)
        return {"status": "started", "round": round_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"aggregation failed: {e}")