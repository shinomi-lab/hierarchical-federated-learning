#!/usr/bin/env python3
"""
3AP 教師データ生成スクリプト (Sim_HFL 用)

M/M/1 + Erlang-B ネットワークモデルでAP条件を生成し、
ハンガリアン法で最適AP割当を計算してCSVに出力する。

実機との対応:
  - 入力特徴量は端末が現在APで実測した measured_tp/measured_rtt
  - start.py _do_inference_snapshot() の inp = [tp, rtt] + one_hot[:4] と同一

出力CSV形式:
  measured_tp, measured_rtt, appIdx, AP0_tp, AP0_rtt, AP0_n, ..., label

使い方:
  python scripts/gen_3ap_training_data.py --output data/3ap_training.csv --rows 3000
"""
import sys
import os
import csv
import argparse
import numpy as np
from scipy.optimize import linear_sum_assignment

# Sim_HFL のモジュールを使うためのパス設定
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sim.network_model import ap_conditions as calc_ap_conditions
from sim.data_gen import terminal_satisfaction, APP_COUNT

# AP 設定 (config/compare_3ap_*.yaml と一致)
AP_PARAMS = [
    {"mu_rtt": 8.0,  "base_rtt_ms": 90.0,  "n_channels": 2,  "tp_init_mbps": 250.0, "rtt_noise_std": 10.0, "tp_noise_std": 1.0},
    {"mu_rtt": 12.0, "base_rtt_ms": 100.0, "n_channels": 5,  "tp_init_mbps": 200.0, "rtt_noise_std": 10.0, "tp_noise_std": 1.0},
    {"mu_rtt": 15.0, "base_rtt_ms": 110.0, "n_channels": 10, "tp_init_mbps": 150.0, "rtt_noise_std": 10.0, "tp_noise_std": 1.0},
]
NUM_APS = 3
NUM_TERMS = 4


def hungarian_optimal(app_indices: list[int], ap_tp_rtt: list[tuple[float, float]]) -> list[int]:
    """
    ハンガリアン法で端末→AP の最適割当を求める。
    コスト = -satisfaction (満足度最大化 → コスト最小化)
    端末数 > AP数の場合、APを仮想的に複製して対応。
    """
    n_terms = len(app_indices)
    n_aps = len(ap_tp_rtt)

    # コスト行列: [n_terms, n_aps]
    cost = np.zeros((n_terms, n_terms), dtype=np.float64)
    for t in range(n_terms):
        for a in range(n_terms):
            ap_idx = a % n_aps  # APを周期的に割り当て
            tp, rtt = ap_tp_rtt[ap_idx]
            sat = terminal_satisfaction(app_indices[t], tp, rtt)
            cost[t, a] = -sat  # 満足度最大化 → コスト最小化

    row_ind, col_ind = linear_sum_assignment(cost)
    assignments = [col_ind[t] % n_aps for t in range(n_terms)]
    return assignments


def main():
    parser = argparse.ArgumentParser(description="3AP教師データ生成")
    parser.add_argument("--output", type=str, default="data/3ap_training.csv")
    parser.add_argument("--rows", type=int, default=3000, help="目標行数")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    rng = np.random.default_rng(args.seed)
    rows_written = 0
    rounds_needed = (args.rows + NUM_TERMS - 1) // NUM_TERMS

    with open(args.output, "w", newline="") as f:
        writer = csv.writer(f)

        for rnd in range(rounds_needed):
            # 各端末にランダムなアプリを割当
            app_indices = rng.integers(0, APP_COUNT, NUM_TERMS).tolist()

            # 現在の接続状態をランダムに生成（各APに0〜NUM_TERMS台）
            current_assignments = rng.integers(0, NUM_APS, NUM_TERMS).tolist()
            n_on_ap = [0] * NUM_APS
            for a in current_assignments:
                n_on_ap[a] += 1

            # 各APのTP/RTT を M/M/1 + Erlang-B で計算（割当前の状態）
            ap_tp_rtt = []
            for ap_idx in range(NUM_APS):
                p = AP_PARAMS[ap_idx]
                tp, rtt = calc_ap_conditions(
                    n_connected=n_on_ap[ap_idx],
                    mu_rtt=p["mu_rtt"],
                    base_rtt_ms=p["base_rtt_ms"],
                    n_channels=p["n_channels"],
                    tp_init_mbps=p["tp_init_mbps"],
                    rng=rng,
                    rtt_noise_std=p["rtt_noise_std"],
                    tp_noise_std=p["tp_noise_std"],
                )
                ap_tp_rtt.append((tp, rtt))

            # ハンガリアン法で最適割当を計算
            optimal = hungarian_optimal(app_indices, ap_tp_rtt)

            # CSV 行を出力
            # 実機と同一: 入力特徴量は端末が現在APで実測した measured_tp/measured_rtt
            for t in range(NUM_TERMS):
                # 端末 t の現在接続 AP での実測値
                cur_ap = current_assignments[t]
                measured_tp, measured_rtt = ap_tp_rtt[cur_ap]
                row = [round(measured_tp, 3), round(measured_rtt, 2), app_indices[t]]
                for ap_idx in range(NUM_APS):
                    tp, rtt = ap_tp_rtt[ap_idx]
                    row.extend([round(tp, 3), round(rtt, 2), n_on_ap[ap_idx]])
                row.append(optimal[t])
                writer.writerow(row)
                rows_written += 1

    print(f"教師データ生成完了: {args.output}")
    print(f"  行数: {rows_written}")
    print(f"  形式: measured_tp, measured_rtt, appIdx, [AP_tp, AP_rtt, AP_n]*{NUM_APS}, label")

    # ラベル分布を表示
    labels = []
    with open(args.output, "r") as f:
        for row in csv.reader(f):
            if row:
                labels.append(int(row[-1]))
    for ap in range(NUM_APS):
        count = labels.count(ap)
        print(f"  AP{ap}: {count} ({count/len(labels)*100:.1f}%)")


if __name__ == "__main__":
    main()
