import io
import torch
import asyncio
import os
import logging
from fastapi import HTTPException, APIRouter
from pydantic import BaseModel, HttpUrl
from central_server.state import global_model_state
from central_server.config import GLOBAL_MODEL_PATH

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class EdgeUpdate(BaseModel):
    edge_url: HttpUrl
    weights: bytes

router = APIRouter()

@router.post("/edge_update")
async def edge_update(req: EdgeUpdate):
    """エッジサーバからの集約済み重みを反映してグローバルモデルを更新"""
    try:
        new_state_dict = await asyncio.to_thread(torch.load, io.BytesIO(req.weights))
    except Exception as e:
        logger.error(f"受信した重みが無効です: {e}")
        raise HTTPException(status_code=400, detail=f"重みが無効です: {e}")

    # 現行グローバルモデルに対して平均を取る
    try:
        if global_model_state["state_dict"] is None:
            global_model_state["state_dict"] = new_state_dict
            return {"status": "global_model_initialized"}

        current_state_dict = global_model_state["state_dict"]
        for k in current_state_dict:
            if k in new_state_dict:
                current_state_dict[k] = (current_state_dict[k] + new_state_dict[k]) / 2
            else:
                raise HTTPException(status_code=400, detail=f"Key {k} not found in new_state_dict")

        global_model_state["state_dict"] = current_state_dict

        # 保存先ディレクトリを作成
        os.makedirs(os.path.dirname(GLOBAL_MODEL_PATH), exist_ok=True)
        buffer = io.BytesIO()
        torch.save(current_state_dict, buffer)
        buffer.seek(0)
        with open(GLOBAL_MODEL_PATH, "wb") as f:
            f.write(buffer.getvalue())

    except Exception as e:
        logger.error(f"グローバルモデルの更新に失敗しました: {e}")
        raise HTTPException(status_code=500, detail=f"グローバルモデルの更新に失敗しました: {e}")

    return {"status": "global_model_updated"}