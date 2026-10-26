#!/usr/bin/env bash
# 奇数N diverse条件実験 — 周期5振動パターン検証
# 予測: N=5, 15 でピーク（5の倍数 = 完全均等配分）
# 予測: N=7 で谷（7%5=2, 3種類が2台ずつ, 2種類が1台）
#
# K_EDGE = floor(N/2) で設定（各エッジの端末数に合わせる）

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS=$(date +%Y%m%d_%H%M%S)
LOGFILE="$REPO/experiment_results/odd_n_diverse_${TS}.log"
CSV_FILE="$REPO/experiment_results/odd_n_diverse_${TS}.csv"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"; }

# CSV ヘッダ
echo "N,K_EDGE,elapsed_s,val_acc_mean,val_acc_std,val_acc_min,val_acc_max" > "$CSV_FILE"

run_trial() {
    local N=$1
    local K=$(( N / 2 ))
    [ "$K" -lt 1 ] && K=1

    log "=============================="
    log "N=$N  K_EDGE=$K  condition=diverse"
    log "予測: N%5=$(( N % 5 ))"
    log "=============================="

    cd "$REPO"

    # ── 前回の残存コンテナを確実に削除 ──
    docker ps -a --format "{{.Names}}" | grep "^hfl-virtterm-" | \
        xargs docker rm -f 2>/dev/null || true

    # ── サーバー起動 ──
    EDGE_AGG_THRESHOLD=$K docker compose -f docker/docker-compose.yml \
        up -d redis central edge-00 edge-01 2>&1 | tail -5 | tee -a "$LOGFILE"
    log "サーバー起動待機 15s..."
    sleep 15

    local START; START=$(date +%s)

    # ── 端末起動 ──
    # EPOCHS=1, LOCAL_ROUNDS=1 に削減（速度優先、相対パターン検証が目的）
    # 元実験(38trials)は LOCAL_ROUNDS=5, EPOCHS=5 だが、
    # 周期5パターンの有無（相対比較）はエポック数に依存しない
    HFL_MODE=async NUM_ROUNDS=30 \
        LOCAL_ROUNDS=1 EPOCHS=1 \
        bash "$REPO/docker/start_mixed_terminals.sh" diverse "$N" internal \
        2>&1 | tee -a "$LOGFILE"

    # 端末コンテナ名を取得
    sleep 3
    local CONTAINERS
    CONTAINERS=$(docker ps --format "{{.Names}}" | grep "^hfl-virtterm-" | sort || true)
    local COUNT; COUNT=$(echo "$CONTAINERS" | grep -c "virtterm" || echo 0)
    log "起動確認: ${COUNT}台 / ${N}台"

    # ── 完了待機 ──
    log "全端末の完了待機中..."
    for c in $CONTAINERS; do
        docker wait "$c" 2>/dev/null || true
    done

    local END ELAPSED
    END=$(date +%s)
    ELAPSED=$(( END - START ))

    # ── val_acc 集計（全端末の avg val_acc） ──
    local ACCS=()
    for c in $CONTAINERS; do
        local ACC
        ACC=$(docker logs "$c" 2>&1 \
            | grep "avg val_acc" | tail -1 \
            | grep -Eo '[0-9]+\.[0-9]+' | tail -1 || echo "")
        if [ -n "$ACC" ]; then
            ACCS+=("$ACC")
            log "  $c: avg val_acc=${ACC}"
        fi
    done

    # Python で平均・std・min・max を計算
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

    log "完了: elapsed=${ELAPSED}s  stats=[mean,std,min,max]=$STATS"
    echo "$N,$K,$ELAPSED,$STATS" >> "$CSV_FILE"

    # ── クリーンアップ ──
    bash "$REPO/docker/stop_mixed_terminals.sh" 2>/dev/null || true
    docker compose -f docker/docker-compose.yml down 2>&1 | tail -3 | tee -a "$LOGFILE"
    log "クリーンアップ完了、5s 休止"
    sleep 5
}

log "=== 奇数N diverse実験開始: $TS ==="
log "目的: i%%5 循環による周期5振動パターンの検証"
log "期待ピーク: N=5(5%%5=0), N=15(15%%5=0)"
log "期待谷:     N=7(7%%5=2), N=13(13%%5=3) — 3種類が過多"
log "ログ: $LOGFILE"
log "CSV:  $CSV_FILE"
log ""

for N in 3 5 7 9 11 13 15; do
    run_trial "$N"
    log ""
done

log "=== 全N完了: $(date '+%Y-%m-%d %H:%M:%S') ==="
log ""
log "--- 結果サマリー ---"
cat "$CSV_FILE" | tee -a "$LOGFILE"
log ""
log "CSVを確認: $CSV_FILE"
