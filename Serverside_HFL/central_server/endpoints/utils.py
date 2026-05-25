from fastapi import APIRouter
from fastapi.responses import JSONResponse
from central_server.utils.marker_store import reset_round

utils_router = APIRouter()


@utils_router.post('/reset_round')
def reset_round_endpoint():
    """Endpoint to reset the round number."""
    try:
        ok = reset_round()
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Exception: {e}"})

    if ok:
        return JSONResponse(status_code=200, content={"status": "success", "message": "Rounds reset successfully."})
    else:
        return JSONResponse(status_code=500, content={"status": "error", "message": "Failed to reset rounds."})