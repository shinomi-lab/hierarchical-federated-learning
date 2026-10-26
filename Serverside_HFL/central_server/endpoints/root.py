# central_server/endpoints/root.py
from fastapi import APIRouter
from starlette.responses import Response

router = APIRouter()

@router.get("/")
async def root():
    return {"message": "中央サーバは稼働中です。"}


@router.head("/")
async def root_head():
    """死活監視の HEAD /（405 を避ける）。"""
    return Response(status_code=200)