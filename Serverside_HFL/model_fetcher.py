# edge_server/model_fetcher.py
import logging
import httpx
from pathlib import Path
from urllib.parse import urljoin

from edge_server.config import CENTRAL_SERVER_URL

# 1. logger変数を正しく定義
logger = logging.getLogger(__name__)

# 2. CURRENT_MODEL_DIR と CURRENT_MODEL_PATH 変数を正しく定義
CURRENT_MODEL_DIR = Path("_current_model")
CURRENT_MODEL_PATH = CURRENT_MODEL_DIR / "latest_model.pt"

async def fetch_and_update_model():
    """
    Fetches the latest global model from the central server and saves it locally.
    """
    # 3. 副作用のある処理はすべて関数の中に移動
    CURRENT_MODEL_DIR.mkdir(exist_ok=True)
    
    # config.pyのCENTRAL_SERVER_URLからベースURLを安全に抽出し、
    # モデル配布エンドポイントの完全なURLを構築します。
    # これにより、CENTRAL_SERVER_URLの末尾のスラッシュの有無などに影響されにくくなります。
    base_url = CENTRAL_SERVER_URL.split('/receive_edge_weights')[0]
    model_url = urljoin(base_url + '/', "global_model/latest")

    try:
        logger.info("Fetching latest model from central server: %s", model_url)
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("GET", model_url) as response:
                response.raise_for_status()
                with open(CURRENT_MODEL_PATH, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        f.write(chunk)
        logger.info("Successfully downloaded and updated model to %s", CURRENT_MODEL_PATH)
        return {"status": "success", "path": str(CURRENT_MODEL_PATH)}
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        logger.error("Failed to fetch model from central server: %s", e, exc_info=True)
        return {"status": "error", "detail": str(e)}
