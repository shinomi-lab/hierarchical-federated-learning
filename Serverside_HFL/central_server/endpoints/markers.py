from fastapi import APIRouter, HTTPException
from typing import Optional
from central_server.utils.marker_store import has_received_hash, has_edge_round, init_marker_db
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/markers/hash/{sha}")
async def check_hash(sha: str):
    """Return whether a given content sha is already recorded on the server."""
    try:
        init_marker_db()
        exists = has_received_hash(sha)
        return {"exists": bool(exists)}
    except Exception as e:
        logger.exception("マーカー確認に失敗しました")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/markers/edge-round")
async def check_edge_round(round: int, edge_id: Optional[str] = None, federation_run_id: Optional[str] = None):
    """Return whether a given (federation_run_id, round, edge_id) has already been recorded."""
    if edge_id is None:
        raise HTTPException(status_code=400, detail="edge_id が必須です")
    try:
        init_marker_db()
        exists = has_edge_round(round, edge_id, federation_run_id)
        return {"exists": bool(exists), "federation_run_id": federation_run_id or "default"}
    except Exception as e:
        logger.exception("マーカー確認に失敗しました")
        raise HTTPException(status_code=500, detail=str(e))
