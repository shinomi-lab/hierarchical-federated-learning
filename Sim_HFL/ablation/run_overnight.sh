#!/usr/bin/env bash
# 夜間用: ①非 IID の 2 条件を各方式 20 試行まで増やす、②AP-B の mu_rtt の 2 条件を各方式 5 試行まで増やす（コンパクト版は 2 試行）、
# 終わったら要約を作る（再開可能: 終わった試行は飛ばす）。②を飛ばすには SKIP_APMUB=1。
#
# 使い方（Mac は電源につないで、蓋を開けたままにする。ディスプレイはスリープしてよい）:
#   cd Sim_HFL && bash ablation/run_overnight.sh            # 実行
#   DRY_RUN=1 bash ablation/run_overnight.sh                # 実行せず、コマンドだけ表示
#   WORKERS=3 REPS=20 bash ablation/run_overnight.sh        # 並列数・試行数の変更
# 実行時間の目安: ①追加 60 試行 ＋ ②追加 12 試行 ＝ 72 試行 ÷ 並列 5 ≒ 15 バッチ × 約 5〜8 分 ≒ 1.5〜2 時間。
# 他に重い作業（Docker・gradle・実機の時間計測）をしていない時間に回す。この実験の指標はステップ数・seed 固定の値なので、
# 他の作業と並行しても結果は変わらない（遅くなるだけ）。ただし実機・Docker の実時間の測定とは同時に回さない。
set -euo pipefail

cd "$(dirname "$0")/.."
REPS="${REPS:-20}"
WORKERS="${WORKERS:-5}"
OUT="${OUT:-results/ablation_20261020}"
ONLY="${ONLY:-data_dir0.5,data_dir0.1}"
STAMP="$(date +%Y%m%d_%H%M)"
LOG="results/ablation_overnight_${STAMP}.log"
REPORT="../experiment_results/sim_ablation_nonIID_${STAMP}.md"
PY=".venv/bin/python3"

run() { if [[ "${DRY_RUN:-}" == "1" ]]; then echo "[dry] $*"; else "$@"; fi; }

if pgrep -f "ablation.run_learning" >/dev/null 2>&1; then
  echo "既に run_learning が動いている。止めてからやり直す。" >&2; exit 1
fi
[[ -x "$PY" ]] || { echo "Sim_HFL/.venv が無い: $PY" >&2; exit 1; }

echo "開始 $(date '+%F %T')  reps=$REPS workers=$WORKERS only=$ONLY out=$OUT" | tee -a "$LOG"
run caffeinate -i "$PY" -m ablation.run_learning --out "$OUT" --reps "$REPS" --workers "$WORKERS" --only "$ONLY" >> "$LOG" 2>&1
echo "①非 IID の実行が終了 $(date '+%F %T')" | tee -a "$LOG"

# ②AP-B の mu_rtt（コンパクト版 各 2 試行 → 各 5 試行）。出力先は別（results/ablation_apmuB_20261021）。
APMUB_OUT="${APMUB_OUT:-results/ablation_apmuB_20261021}"
if [[ "${SKIP_APMUB:-}" != "1" ]]; then
  run caffeinate -i "$PY" -m ablation.run_learning --out "$APMUB_OUT" --reps 5 --workers "$WORKERS" --only apmuB_mu2.5,apmuB_mu10 >> "$LOG" 2>&1
  echo "②AP-B の実行が終了 $(date '+%F %T')" | tee -a "$LOG"
fi

# 要約（非 IID の追加分に加え、基準・速度・AP の既存結果も同じ表に入れる）
CSVS=("$OUT/learning_raw.csv")
[[ -f results/ablation_apmu_20261021/learning_raw.csv ]] && CSVS+=("results/ablation_apmu_20261021/learning_raw.csv")
[[ -f "$APMUB_OUT/learning_raw.csv" ]] && CSVS+=("$APMUB_OUT/learning_raw.csv")
run "$PY" -m ablation.summarize "${CSVS[@]}" --out "$REPORT"
echo "要約: $REPORT  ログ: $LOG" | tee -a "$LOG"
