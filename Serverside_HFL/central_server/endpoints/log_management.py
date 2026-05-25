from fastapi import APIRouter, Request
from datetime import datetime
import logging
from logging.handlers import FileHandler
import os
from pathlib import Path

router = APIRouter()

# ログファイルの設定
log_dir = Path(__file__).resolve().parent.parent / 'logs' / 'time_records'
log_dir.mkdir(parents=True, exist_ok=True)
log_file = str(log_dir / f"central_server_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
file_handler = FileHandler(log_file, mode='a', delay=False)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# ロガーの設定
logger = logging.getLogger("central_server")
logger.setLevel(logging.INFO)
if not logger.handlers:  # ハンドラーが重複しないように確認
    logger.addHandler(file_handler)

logger.info("中央サーバのログ（タイムレコード用ファイルハンドラ）を初期化しました。")

@router.post("/create_log")
async def create_log(request: Request):
    data = await request.json()
    timestamp = data.get("timestamp")
    if not timestamp:
        return {"error": "timestamp_required", "message": "タイムスタンプが必要です"}

    # Create a new log file with the provided timestamp
    log_file = f"logs/time_records/central_server_{timestamp}.log"
    if not os.path.exists(log_file):  # Check if the file already exists
        with open(log_file, "w") as f:
            f.write("タイムスタンプ付きでログファイルを作成しました。\n")
        logging.info(f"ログファイルを作成しました: {log_file}")
    else:
        logging.info(f"ログファイルは既に存在します: {log_file}")

    return {"status": "success", "log_file": log_file}