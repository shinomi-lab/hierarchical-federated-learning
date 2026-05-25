#!/usr/bin/env bash
# bash版: central_server を起動してテストを実行する
# ※ 元のPowerShell版 start_central_and_run_test.ps1 をbashに変換したもの

set -euo pipefail

# スクリプトのディレクトリを基点にプロジェクトルートへ移動
cd "$(dirname "$0")/.."

# Mac 向け環境変数（AppData/Local相当 → ~/Library/Application Support）
export MARKER_DB_PATH="${HOME}/Library/Application Support/hfl_markers/markers.db"
export HFL_STORAGE_DIR="${HOME}/Library/Application Support/hfl_data"

echo "Env set:"
echo "  MARKER_DB_PATH=${MARKER_DB_PATH}"
echo "  HFL_STORAGE_DIR=${HFL_STORAGE_DIR}"

# ポート 8000 を使用中のプロセスを停止
PID=$(lsof -ti tcp:8000 2>/dev/null || true)
if [[ -n "$PID" ]]; then
    echo "Killing process on port 8000: ${PID}"
    kill -9 "$PID" 2>/dev/null || true
else
    echo "No process found on port 8000"
fi

echo "Starting uvicorn in background..."
python -m uvicorn central_server.main:app \
    --host 0.0.0.0 --port 8000 \
    --timeout-keep-alive 120 &
UVICORN_PID=$!
echo "uvicorn started (PID: ${UVICORN_PID})"
sleep 4

echo "Running 50MB two-run test script (this may take a while)"
bash ./scripts/run_50MB_test.sh

echo "Test finished, check logs in logs/time_records and scripts output."
