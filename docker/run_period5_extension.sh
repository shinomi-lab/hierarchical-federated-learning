#!/usr/bin/env bash
# 第5周期拡張実験: N=21〜25 diverse条件
# 目的: 周期5則が第5周期(N=21-25)でも成立するか検証
#   N=25(25%5=0): ピーク期待
#   N=22(22%5=2): 谷期待
#   N=23(23%5=3): 谷期待
#
# 設定: run_supplement_full_sweep.sh と同一 (EPOCHS=1, LOCAL_ROUNDS=1, UNIFORM_CPU=1.0)
# 所要時間見積: 各N=600〜900s × 5本 ≒ 1時間

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS=$(date +%Y%m%d_%H%M%S)
LOGFILE="$REPO/experiment_results/period5_ext_${TS}.log"
CSV_FILE="$REPO/experiment_results/period5_ext_${TS}.csv"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"; }

echo "N,K_EDGE,elapsed_s,val_acc_mean,val_acc_std,val_acc_min,val_acc_max" > "$CSV_FILE"

run_trial() {
    local N=$1
    local K=$(( N / 2 ))
    [ "$K" -lt 1 ] && K=1

    log "=============================="
    log "N=$N  K_EDGE=$K  N%%5=$(( N % 5 ))"
    log "=============================="

    cd "$REPO"

    # 残存コンテナ削除
    docker ps -a --format "{{.Names}}" | grep "^hfl-virtterm-" | \
        xargs docker rm -f 2>/dev/null || true

    # サーバー起動
    EDGE_AGG_THRESHOLD=$K docker compose -f docker/docker-compose.yml \
        up -d redis central edge-00 edge-01 2>&1 | tail -4 | tee -a "$LOGFILE"
    log "サーバー起動待機 15s..."
    sleep 15

    local START; START=$(date +%s)

    # 端末起動 (full_sweepと同一設定)
    HFL_MODE=async NUM_ROUNDS=30 LOCAL_ROUNDS=1 EPOCHS=1 \
        UNIFORM_CPU=1.0 \
        bash "$REPO/docker/start_mixed_terminals.sh" diverse "$N" internal \
        2>&1 | tee -a "$LOGFILE"

    sleep 3
    local CONTAINERS
    CONTAINERS=$(docker ps --format "{{.Names}}" | grep "^hfl-virtterm-" | sort || true)
    local COUNT; COUNT=$(echo "$CONTAINERS" | grep -c "virtterm" 2>/dev/null || echo 0)
    log "起動確認: ${COUNT}台 / ${N}台"

    log "全端末の完了待機中..."
    for c in $CONTAINERS; do
        docker wait "$c" 2>/dev/null || true
    done

    local END ELAPSED
    END=$(date +%s)
    ELAPSED=$(( END - START ))

    local ACCS=()
    for c in $CONTAINERS; do
        local ACC
        ACC=$(docker logs "$c" 2>&1 \
            | grep "avg val_acc" | tail -1 \
            | grep -Eo '[0-9]+\.[0-9]+' | tail -1 || echo "")
        if [ -n "$ACC" ]; then
            ACCS+=("$ACC")
            log "  $c: avg_val_acc=${ACC}"
        fi
    done

    local STATS
    STATS=$(python3 - "${ACCS[@]:-}" <<'PYEOF'
import sys, statistics
vals = [float(x) for x in sys.argv[1:] if x]
if not vals:
    print("N/A,N/A,N/A,N/A")
elif len(vals) == 1:
    v = vals[0]
    print(f"{v:.4f},0.0000,{v:.4f},{v:.4f}")
else:
    mean = statistics.mean(vals)
    std  = statistics.stdev(vals)
    print(f"{mean:.4f},{std:.4f},{min(vals):.4f},{max(vals):.4f}")
PYEOF
)

    log "完了: elapsed=${ELAPSED}s  [mean,std,min,max]=$STATS"
    echo "$N,$K,$ELAPSED,$STATS" >> "$CSV_FILE"

    bash "$REPO/docker/stop_mixed_terminals.sh" 2>/dev/null || true
    docker compose -f docker/docker-compose.yml down 2>&1 | tail -3 | tee -a "$LOGFILE"
    log "クリーンアップ完了、5s 休止"
    sleep 5
}

log "=== 第5周期拡張実験 (N=21〜25) 開始: $TS ==="
log "設定: diverse, EPOCHS=1, LOCAL_ROUNDS=1, NUM_ROUNDS=30, UNIFORM_CPU=1.0"
log "参照: full_sweep_20260702_225720.csv (N=2〜20 の結果と同一設定)"
log ""
log "周期5予測:"
log "  N=21 (21%%5=1): ~0.794 (高め)"
log "  N=22 (22%%5=2): ~0.769 (低め)"
log "  N=23 (23%%5=3): ~0.761 (低め)"
log "  N=24 (24%%5=4): ~0.792 (中高)"
log "  N=25 (25%%5=0): ~0.796 (ピーク期待)"
log "ログ: $LOGFILE"
log "CSV:  $CSV_FILE"
log ""

for N in 21 22 23 24 25; do
    run_trial "$N"
    log ""
done

log "=== 全N完了: $(date '+%Y-%m-%d %H:%M:%S') ==="
log ""
log "--- 結果サマリー ---"
cat "$CSV_FILE" | tee -a "$LOGFILE"
log ""
log "次のステップ: full_sweep と結合して N=2〜25 の連続グラフを描画"
log "  python3 experiment_results/plot_scale_curves.py --include-period5"
