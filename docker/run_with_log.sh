#!/usr/bin/env bash
# HFL: docker compose up のターミナル出力をログに残す。
# ・本文は tee で逐次追記（長時間実行や異常終了でもできるだけ残す）
# ・停止時（正常終了 / Ctrl+C / docker compose の終了）にフッタを追記して締める
#
# 使い方（リポジトリルートから）:
#   bash docker/run_with_log.sh
#   bash docker/run_with_log.sh --no-build
#
# ログ: docker/logs/compose_YYYYMMDD_HHMMSS.log
set -euo pipefail
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

TS="$(date +%Y%m%d_%H%M%S)"
LOGFILE="$LOG_DIR/compose_${TS}.log"

FINALIZED=0
cleanup() {
  (($FINALIZED)) && return
  FINALIZED=1
  {
    echo ""
    echo "=== セッション終了（フッタは停止時に追記） ==="
    echo "終了(ローカル時刻): $(date '+%Y-%m-%d %H:%M:%S %z')"
  } >>"$LOGFILE" 2>/dev/null || true
  sync 2>/dev/null || true
  echo "ログを締めました: $LOGFILE"
}

trap cleanup EXIT

BUILD=("--build")
if [[ "${1:-}" == "--no-build" ]]; then
  BUILD=()
fi

{
  echo "=== HFL Docker compose セッションログ ==="
  echo "開始(ローカル時刻): $(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "ログファイル: $LOGFILE"
  echo "リポジトリ: $REPO_ROOT"
  if ((${#BUILD[@]})); then echo "コマンド: docker compose up --build"; else echo "コマンド: docker compose up"; fi
  echo "（本文は逐次このファイルへ追記。停止時に末尾へフッタを追記します）"
  echo "----------------------------------------"
} | tee "$LOGFILE"

cd "$REPO_ROOT"
set +e
docker compose up "${BUILD[@]}" 2>&1 | tee -a "$LOGFILE"
compose_exit="${PIPESTATUS[0]}"
set -e
exit "${compose_exit}"
