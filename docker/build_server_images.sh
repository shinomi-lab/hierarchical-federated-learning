#!/bin/bash
# docker/build_server_images.sh — 中央・エッジのイメージを「コミット済みのコードだけ」から作る。
#
# Dockerfile.central/edge は COPY . /app/ なので、Serverside_HFL をそのまま文脈にすると .venv・過去の受信モデル
# （出力 4 クラス時代を含む）・DB・edge_server/runs（ラウンドを引き継ぐ）までイメージに入り、古いモデルが配られうる。
# ここでは git archive HEAD から実行時データを除いた文脈で作り、compose が使う名前（hfl-<service>）で tag する。
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTX="$(mktemp -d)/ctx"
mkdir -p "$CTX"
cd "$REPO"
git archive HEAD -- Serverside_HFL \
  ':(exclude)Serverside_HFL/received_files' \
  ':(exclude)Serverside_HFL/received_edges' \
  ':(exclude)Serverside_HFL/logs' \
  ':(exclude)Serverside_HFL/archives' \
  ':(exclude)Serverside_HFL/edge_server/runs' \
  ':(exclude)Serverside_HFL/edge_server/_aggregation_cache' \
  ':(exclude)Serverside_HFL/central_server/state' \
  ':(exclude)Serverside_HFL/Notion-0501' \
  ':(exclude)Serverside_HFL/Notion-0508' \
  ':(exclude)Serverside_HFL/edge_logs_20251124.zip' \
  | tar -x -C "$CTX"
S="$CTX/Serverside_HFL"
mkdir -p "$S/central_server/state"
echo "context: $S ($(du -sh "$S" | cut -f1)) commit=$(git rev-parse --short HEAD)"
docker build -q -t hfl-central -f docker/Dockerfile.central "$S"
docker build -q -t hfl-edge-00 -f docker/Dockerfile.edge "$S"
docker tag hfl-edge-00 hfl-edge-01
docker image ls --format '{{.Repository}} {{.CreatedSince}} {{.Size}}' | grep -E '^hfl-(central|edge)'
