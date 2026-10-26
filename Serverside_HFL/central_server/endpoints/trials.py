# central_server/endpoints/trials.py
"""REST endpoints for trial (experiment run) management."""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from typing import Optional
from central_server.utils.trial_registry import (
    start_trial, end_trial, update_trial, get_trial, list_trials, get_latest_trial,
)

router = APIRouter(prefix="/api/v1/trials", tags=["trials"])


@router.post("/start")
async def api_start_trial(
    num_rounds: int = 0,
    num_edges: int = 0,
    num_terminals: int = 0,
    description: str = "",
):
    trial_id = start_trial(
        num_rounds=num_rounds,
        num_edges=num_edges,
        num_terminals=num_terminals,
        description=description,
    )
    return {"status": "ok", "trial_id": trial_id}


@router.post("/end")
async def api_end_trial(
    trial_id: str,
    status: str = "completed",
):
    end_trial(trial_id, status=status)
    return {"status": "ok", "trial_id": trial_id}


@router.get("/list")
async def api_list_trials(
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    return list_trials(status=status, limit=limit)


@router.get("/latest")
async def api_latest_trial():
    t = get_latest_trial()
    if t is None:
        return JSONResponse(status_code=404, content={"detail": "No trials found"})
    return t


@router.get("/{trial_id}")
async def api_get_trial(trial_id: str):
    t = get_trial(trial_id)
    if t is None:
        return JSONResponse(status_code=404, content={"detail": f"Trial {trial_id} not found"})
    return t
