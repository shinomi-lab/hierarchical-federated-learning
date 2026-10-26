#!/bin/bash
# 実機エッジまたは Docker 内部ネットワークに Docker 仮想端末を接続する
# 使い方:
#   bash start_mixed_terminals.sh <condition> <num_terminals> [edge_ip_or_internal]
# 条件:
#   same     : 全端末が default データ
#   diverse  : 5種類のバリエーションを分散配布
# ターゲット:
#   IP アドレス (例: 192.168.11.2) - 実機LAN経由
#   internal - Docker 内部ネットワーク (hfl_hfl_net) 経由

CONDITION=${1:-same}
NUM_TERMINALS=${2:-8}
TARGET=${3:-internal}
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DATA_DIRS=(
    "$REPO_ROOT/training_data_list/default"
    "$REPO_ROOT/training_data_list/100_100"
    "$REPO_ROOT/training_data_list/200_100"
    "$REPO_ROOT/training_data_list/200_20"
    "$REPO_ROOT/training_data_list/200_250"
)

APPS=("browser" "video" "call" "other")

echo "=== 混合HFL: Docker仮想端末起動 ==="
echo "  Condition: $CONDITION"
echo "  Terminals: $NUM_TERMINALS"
echo "  Target:    $TARGET"
echo ""

# Docker イメージ確認
if ! docker image inspect hfl-terminal-00 >/dev/null 2>&1; then
    echo "ERROR: hfl-terminal-00 イメージがありません。"
    exit 1
fi

# ネットワーク引数を決定
if [ "$TARGET" = "internal" ]; then
    NETWORK_ARG="--network hfl_hfl_net"
    EDGE_URL_1="http://edge-00:8001"
    EDGE_URL_2="http://edge-01:8001"
else
    NETWORK_ARG=""
    EDGE_URL_1="http://$TARGET:8001"
    EDGE_URL_2="http://$TARGET:8002"
fi

for i in $(seq 0 $((NUM_TERMINALS - 1))); do
    TID="virtterm-$(printf %02d $i)"
    APP_IDX=$((i % 4))
    APP=${APPS[$APP_IDX]}

    # データ配分
    if [ "$CONDITION" = "same" ]; then
        DATA_DIR="${DATA_DIRS[0]}"
    else
        DATA_IDX=$((i % ${#DATA_DIRS[@]}))
        DATA_DIR="${DATA_DIRS[$DATA_IDX]}"
    fi

    # エッジ振り分け（半分ずつ）
    if [ $i -lt $((NUM_TERMINALS / 2)) ]; then
        EDGE_URL=$EDGE_URL_1
    else
        EDGE_URL=$EDGE_URL_2
    fi

    # CPU 割当（UNIFORM_CPU 設定時は全端末同一に）
    if [ -n "${UNIFORM_CPU:-}" ]; then
        CPUS="$UNIFORM_CPU"
    else
        case $((i % 5)) in
            0) CPUS="1.0" ;;
            1) CPUS="0.7" ;;
            2) CPUS="0.5" ;;
            3) CPUS="0.8" ;;
            4) CPUS="0.6" ;;
        esac
    fi

    echo "  $TID → $EDGE_URL (app=$APP, data=$(basename $DATA_DIR), cpu=$CPUS)"

    docker run -d \
        $NETWORK_ARG \
        --name "hfl-$TID" \
        --cpus="$CPUS" \
        -e TERMINAL_ID="$TID" \
        -e EDGE_URL="$EDGE_URL" \
        -e APP_CATEGORY="$APP" \
        -e HFL_MODE="${HFL_MODE:-async}" \
        -e SEED=$i \
        -e NUM_ROUNDS="${NUM_ROUNDS:-30}" \
        -e LOCAL_ROUNDS="${LOCAL_ROUNDS:-5}" \
        -e EPOCHS="${EPOCHS:-5}" \
        -e CSV_PATH="/app/training_data/training_data.csv" \
        -v "$DATA_DIR:/app/training_data:ro" \
        hfl-terminal-00 > /dev/null

    sleep 0.5
done

echo ""
echo "全 $NUM_TERMINALS 端末を起動しました"
