#!/usr/bin/env bash
# 論理クライアント（仮想端末）1 台分のメモリを docker stats で確認する。
#
# 使い方（リポジトリルートで）:
#   bash docker/measure_terminal_mem.sh              # 1 台起動して即時 1 回表示
#   bash docker/measure_terminal_mem.sh watch       # 1 秒ごとに更新（Ctrl+C で終了）
#   bash docker/measure_terminal_mem.sh stats-only    # 既に hfl-terminal-00 が動いている前提で 1 回だけ
#
# 前提: エッジはホスト等で既に LISTEN している（論理 compose 既定は host.docker.internal:8001）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE=(docker compose -f "$SCRIPT_DIR/docker-compose.logical-clients.yml")
NAME="hfl-terminal-00"

mode="${1:-}"

stats_once() {
  docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}\t{{.NetIO}}\t{{.BlockIO}}" "$NAME" 2>/dev/null \
    || docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}\t{{.NetIO}}\t{{.BlockIO}}" "$(docker ps -q -f name=$NAME)"
}

case "$mode" in
  stats-only)
    stats_once
    ;;
  watch)
    echo "1 秒ごとに更新（Ctrl+C で終了）。watch コマンド不要版。"
    while true; do
      clear 2>/dev/null || true
      date "+%H:%M:%S"
      stats_once || true
      sleep 1
    done
    ;;
  ""|up)
    cd "$REPO_ROOT"
    "${COMPOSE[@]}" up -d terminal-00
    echo "コンテナ起動直後:"
    sleep 2
    stats_once
    echo ""
    echo "学習が進むとメモリが伸びる場合があります。追跡するには:"
    echo "  bash docker/measure_terminal_mem.sh watch"
    echo "止めるには: docker compose -f docker/docker-compose.logical-clients.yml stop"
    ;;
  *)
    echo "Usage: $0 [up|watch|stats-only]" >&2
    exit 1
    ;;
esac
