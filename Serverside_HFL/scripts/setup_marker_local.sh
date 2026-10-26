#!/usr/bin/env bash
# bash版: ローカルの marker DB をセットアップする
# ※ 元のPowerShell版 setup_marker_local.ps1 をbashに変換したもの

set -euo pipefail

# スクリプトのディレクトリを基点にプロジェクトルートへ移動
cd "$(dirname "$0")/.."

TARGET_DIR="${HOME}/Library/Application Support/hfl_markers"
TARGET_DB="markers.db"
FULL_PATH="${TARGET_DIR}/${TARGET_DB}"

echo "Preparing marker DB target: ${FULL_PATH}"
mkdir -p "${TARGET_DIR}"

echo "Invoking migration helper via python..."
python3 - <<PYEOF
import sys
sys.path.insert(0, '.')
from pathlib import Path
from central_server.utils.marker_store import migrate_marker_db
result = migrate_marker_db(Path('${FULL_PATH}'))
print('migrate result:', result)
PYEOF

STORAGE_DIR="${HOME}/Library/Application Support/hfl_data"

echo ""
echo "If migration succeeded, set these env vars before starting central_server:"
echo "  export MARKER_DB_PATH='${FULL_PATH}'"
echo "  export HFL_STORAGE_DIR='${STORAGE_DIR}'"
echo ""
echo "You can add them to your ~/.zshrc or set for the current session like this:"
echo "  export MARKER_DB_PATH='${FULL_PATH}'"
echo "  export HFL_STORAGE_DIR='${STORAGE_DIR}'"
echo ""
echo "Then restart central_server process (stop and start uvicorn)."
echo "Also consider running: python3 ./scripts/check_marker_smoke.py to verify backend operations."
