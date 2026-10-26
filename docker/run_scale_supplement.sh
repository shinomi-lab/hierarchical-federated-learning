#!/usr/bin/env bash
# B実験: N=10,14 Async/Sync 試行数追加
# 合成データ1000サンプル使用（元スケール実験と同条件）
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGFILE="$REPO_ROOT/experiment_results/scale_supplement_$(date +%Y%m%d_%H%M%S).log"

log() { echo "$*" | tee -a "$LOGFILE"; }

# CPUマップ（docker-compose.ymlと同一設定）
cpu_for() {
    case $(( $1 % 5 )) in
        0) echo "1.0" ;; 1) echo "0.7" ;; 2) echo "0.5" ;;
        3) echo "0.8" ;; 4) echo "0.6" ;;
    esac
}

# エッジURL割当（N/2台ずつ edge-00/edge-01 に分散）
edge_for() {
    local i=$1 N=$2
    if [ "$i" -lt $(( N / 2 )) ]; then echo "http://edge-00:8001"
    else echo "http://edge-01:8001"; fi
}

APPS=("browser" "video" "call" "other")

run_trial() {
    local N=$1 MODE=$2 K=$3 RUN=$4

    log ""
    log "=========================================="
    log "Trial: N=$N  mode=$MODE  K_EDGE=$K  run=$RUN"
    log "開始: $(date '+%Y-%m-%d %H:%M:%S')"
    log "=========================================="

    cd "$REPO_ROOT"

    # ── サーバー起動 ──
    EDGE_AGG_THRESHOLD=$K docker compose -f docker/docker-compose.yml \
        up -d redis central edge-00 edge-01
    log "サーバー起動中..."
    sleep 20

    local START
    START=$(date +%s)

    # ── 端末起動（docker run、CSVなし=合成データ） ──
    local CONTAINER_NAMES=()
    for i in $(seq 0 $(( N - 1 ))); do
        local TID="virtterm-$(printf '%02d' $i)"
        local APP="${APPS[$((i % 4))]}"
        local CPUS; CPUS=$(cpu_for "$i")
        local EDGE; EDGE=$(edge_for "$i" "$N")
        local CNAME="hfl-scale-$TID"

        docker run -d \
            --name "$CNAME" \
            --network hfl_hfl_net \
            --cpus="$CPUS" \
            -e TERMINAL_ID="$TID" \
            -e EDGE_URL="$EDGE" \
            -e APP_CATEGORY="$APP" \
            -e HFL_MODE="$MODE" \
            -e SEED="$i" \
            -e NUM_ROUNDS=30 \
            -e NUM_SAMPLES=1000 \
            hfl-terminal-00 > /dev/null

        CONTAINER_NAMES+=("$CNAME")
        log "  起動: $CNAME (cpu=$CPUS app=$APP edge=$EDGE)"
    done

    log "全 $N 台起動完了、完了待機中..."
    docker wait "${CONTAINER_NAMES[@]}" 2>/dev/null || true

    local END ELAPSED
    END=$(date +%s)
    ELAPSED=$(( END - START ))

    # ── 結果取得 ──
    local VAL_ACC
    VAL_ACC=$(docker logs "${CONTAINER_NAMES[0]}" 2>&1 \
        | grep -oP 'val_acc=\K[0-9.]+' | tail -1 || echo "N/A")

    log "完了: elapsed=${ELAPSED}s  val_acc=${VAL_ACC}"
    echo "B,${MODE},${N},${K},${RUN},${ELAPSED},${VAL_ACC}" >> "$LOGFILE"

    # ── クリーンアップ ──
    for c in "${CONTAINER_NAMES[@]}"; do
        docker rm -f "$c" 2>/dev/null || true
    done
    docker compose -f docker/docker-compose.yml down
    log "クリーンアップ完了"
    sleep 5
}

log "=== B実験開始 (合成データ1000samples): $(date '+%Y-%m-%d %H:%M:%S') ==="
log "ログ: $LOGFILE"

run_trial 10 async 5 1
run_trial 10 async 5 2

run_trial 10 sync  5 1
run_trial 10 sync  5 2

run_trial 14 async 7 1
run_trial 14 async 7 2

run_trial 14 sync  7 1

log ""
log "=== 全試行完了: $(date '+%Y-%m-%d %H:%M:%S') ==="
log "結果: $LOGFILE"
