# central_server/utils/validation.py

from fastapi import HTTPException


def validate_edge_url(edge_url: str) -> None:
    """エッジサーバのURLが有効か簡易チェック"""
    if not edge_url.startswith("http"):
        raise HTTPException(status_code=400, detail="edge_url は http または https で始まる必要があります")


def validate_weight_bytes(data: bytes) -> None:
    """重みデータが空でないか確認"""
    if not data:
        raise HTTPException(status_code=400, detail="重みデータが空です")
