#!/bin/bash
# 混合HFL用の Docker 仮想端末を全て停止・削除
CONTAINERS=$(docker ps -a --format "{{.Names}}" | grep "^hfl-virtterm-")
if [ -z "$CONTAINERS" ]; then
    echo "hfl-virtterm-* コンテナが見つかりません"
    exit 0
fi
echo "停止中: $CONTAINERS" | tr ' ' '\n'
docker rm -f $CONTAINERS
