#!/usr/bin/env bash
# 補足実験: 偶数N再測定 (EPOCHS=1統一) + 第3周期拡張 (N=16〜20)
#
# 目的:
#   1. 偶数N (N=2,4,6,8,10,12,14) を EPOCHS=1 で再測定
#      → 奇数N実験と同一設定にし、N=2〜15 の連続グラフを完成させる
#   2. N=16〜20 まで拡張
#      → N=20(20%5=0) でピークが出れば周期5則が3周期にわたって成立
#
# 所要時間見積もり:
#   UNIFORM_CPU=1.0 で全端末を均一速度に統一
#   1ラウンド約8〜10秒 × 30rounds = 約5分/試行
#   偶数N 7本 × 5分 = 35分
#   N=16〜20 5本 × 8分 = 40分（大きいNは並列コンテナ増で若干重い）
#   合計 約1時間30分（2時間以内）
#
# ※ CPU異種性は本補足では除外（データ多様性効果のみ測定）
# ※ 奇数N実験(odd_n_diverse_*.csv)と組み合わせてN=2〜20の連続曲線を描く
#
# EPOCHS=1 LOCAL_ROUNDS=1 NUM_ROUNDS=30 (奇数N実験と同一設定)
# 38試行(EPOCHS=5)との絶対値比較は不可。相対パターン検証が目的。

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS=$(date +%Y%m%d_%H%M%S)
LOGFILE="$REPO/experiment_results/full_sweep_${TS}.log"
CSV_FILE="$REPO/experiment_results/full_sweep_${TS}.csv"

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

    # 端末起動 (均一CPU=1.0 + EPOCHS=1 LOCAL_ROUNDS=1 NUM_ROUNDS=30)
    # UNIFORM_CPU=1.0: 全端末を均一速度にし cpu=0.5 の律速を排除
    # → 1ラウンド約8〜10秒、30ラウンド × 10s = 約5分/試行
    # 異種速度は本補足では非測定（データ多様性効果のみに集中）
    HFL_MODE=async NUM_ROUNDS=30 LOCAL_ROUNDS=1 EPOCHS=1 \
        UNIFORM_CPU=1.0 \
        bash "$REPO/docker/start_mixed_terminals.sh" diverse "$N" internal \
        2>&1 | tee -a "$LOGFILE"

    sleep 3
    local CONTAINERS
    CONTAINERS=$(docker ps --format "{{.Names}}" | grep "^hfl-virtterm-" | sort || true)
    local COUNT; COUNT=$(echo "$CONTAINERS" | grep -c "virtterm" 2>/dev/null || echo 0)
    log "起動確認: ${COUNT}台 / ${N}台"

    # 全端末完了待機
    log "完了待機中..."
    for c in $CONTAINERS; do
        docker wait "$c" 2>/dev/null || true
    done

    local END ELAPSED
    END=$(date +%s)
    ELAPSED=$(( END - START ))

    # val_acc 集計
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

    # クリーンアップ
    bash "$REPO/docker/stop_mixed_terminals.sh" 2>/dev/null || true
    docker compose -f docker/docker-compose.yml down 2>&1 | tail -3 | tee -a "$LOGFILE"
    log "クリーンアップ完了、5s 休止"
    sleep 5
}

log "=== 補足実験 (full_sweep) 開始: $TS ==="
log "設定: diverse条件, EPOCHS=1, LOCAL_ROUNDS=1, NUM_ROUNDS=30"
log ""
log "--- Part 1: 偶数N (奇数Nと同設定で比較基盤) ---"
log "N=2,4,6,8,10,12,14 → 奇数N結果と結合してN=2〜15の連続曲線を描く"
log ""

for N in 2 4 6 8 10 12 14; do
    run_trial "$N"
    log ""
done

log "--- Part 2: 第3周期拡張 N=16〜20 ---"
log "N=20(20%%5=0) でピークが出れば周期5則が3周期にわたって成立"
log ""

for N in 16 17 18 19 20; do
    run_trial "$N"
    log ""
done

log "=== 全N完了: $(date '+%Y-%m-%d %H:%M:%S') ==="
log ""
log "--- 結果サマリー ---"
cat "$CSV_FILE" | tee -a "$LOGFILE"
log ""
log "次のステップ: 奇数N結果 (odd_n_diverse_*.csv) と結合して N=2〜20 の連続グラフを描画"
