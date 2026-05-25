# edge_server/utils/file_utils.py

import os
from pathlib import Path
from typing import Optional
import aiofiles

async def save_uploaded_file(file, save_dir: Path) -> Path:
    """
    アップロードされたファイルを指定ディレクトリに保存する。
    非同期に読み取り・書き込みを行う。
    """
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / file.filename
    content = await file.read()
    
    async with aiofiles.open(save_path, "wb") as f:
        await f.write(content)

    return save_path



def get_latest_batch_dir(received_dir: str) -> str:
    """
    指定されたディレクトリ内で最新のバッチディレクトリを取得する。
    """
    try:
        dirs = [os.path.join(received_dir, d) for d in os.listdir(received_dir) if os.path.isdir(os.path.join(received_dir, d))]
        latest_dir = max(dirs, key=os.path.getmtime)
        return latest_dir
    except ValueError:
        return None
