"""
sim/data_gen.py
---------------
AP 選択タスク用の合成データセット生成器。
実機 (start.py _do_inference_snapshot + SimpleDataLoader.loadFlexible) に準拠。

特徴量 (6次元 / 15次元):
  ── 基本 6次元 ──
  x[0]: needTP_norm   アプリの要求TP（正規化） [0, 1]
  x[1]: needRTT_norm  アプリの要求RTT（正規化） [0, 1]
  x[2]: app_browser  one-hot
  x[3]: app_video    one-hot
  x[4]: app_call     one-hot
  x[5]: app_other    one-hot

  ── AP状態拡張 (use_ap_state=True 時に追加) ──
  x[6]:  AP0_tp_norm    AP0のTP（正規化）
  x[7]:  AP0_rtt_norm   AP0のRTT（正規化）
  x[8]:  AP0_n_norm     AP0の接続台数（正規化）
  x[9]:  AP1_tp_norm
  x[10]: AP1_rtt_norm
  x[11]: AP1_n_norm
  x[12]: AP2_tp_norm
  x[13]: AP2_rtt_norm
  x[14]: AP2_n_norm

ラベル:
  y ∈ {0, 1, ...} = 接続すべき最適な AP の ID

CSV 形式:
  6次元用: needTP, needRTT, appIdx, label  (4列)
  15次元用: needTP, needRTT, appIdx, AP0_tp, AP0_rtt, AP0_n, ..., label (4 + 3*N_AP 列)

IID / non-IID 分割:
  iid=True  → 各端末に均等ランダム分割
  iid=False → ディリクレ分布でラベル偏りを持たせた分割
"""
from __future__ import annotations

import numpy as np
import torch
from torch.utils.data import Dataset, Subset
from typing import List, Tuple

# ------------------------------------------------------------------ #
# アプリカテゴリ定義（実機 APP_CAT_COUNT = 4 と一致）
# ------------------------------------------------------------------ #
APP_TYPES = ["browser", "video", "call", "other"]
APP_COUNT = len(APP_TYPES)

# 各アプリの QoS 要件
_TP_NEEDS  = [5.0,  2.0, 10.0,  1.0]   # Mbps
_RTT_NEEDS = [100.0, 50.0, 200.0, 30.0]  # ms
_RTT_PRIOR = {0, 1, 2, 3}   # 全アプリ RTT 優先（実機の遅延環境に合わせる）

def terminal_satisfaction(app_idx: int, tp: float, rtt: float) -> float:
    """実機 TerminalSatisfaction.calculateSatisfaction() と同一式"""
    eps = 1e-6
    if app_idx in _RTT_PRIOR:
        raw = _RTT_NEEDS[app_idx] / max(rtt, eps)
    else:
        raw = tp / max(_TP_NEEDS[app_idx], eps)
    return float(min(1.0, max(eps, raw)))



class APSelectionDataset(Dataset):
    """
    外部 CSV ファイルからデータを読み込むデータセットクラス。

    CSV 形式:
      4列:  measured_tp, measured_rtt, appIdx, label                        → 6次元特徴量
      4+3N列: measured_tp, measured_rtt, appIdx, [AP_tp, AP_rtt, AP_n]*N, label → 6+3N次元特徴量

    measured_tp/measured_rtt: 端末が現在APで実測した TP(Mbps) / RTT(ms)。
    実機 start.py の _do_inference_snapshot() と同一入力形式。

    use_ap_state=True の場合、AP状態列を特徴量に含める（15次元等）。
    use_ap_state=False の場合、AP状態列を無視して6次元特徴量のみ。
    """
    TP_MAX  = 60.0
    RTT_MAX = 2000.0
    # AP状態の正規化定数
    AP_TP_MAX  = 300.0   # Mbps
    AP_RTT_MAX = 2000.0  # ms
    AP_N_MAX   = 10.0    # 端末台数

    def __init__(
        self,
        csv_path: str = None,
        n_samples: int = None,
        seed: int = 42,
        use_ap_state: bool = False,
        num_aps: int = 3,
    ) -> None:
        import csv
        import os

        if csv_path is None:
            csv_path = "/Users/tetsuya/HFL/training_data_list/default/training_data.csv"

        if not os.path.exists(csv_path):
            raise FileNotFoundError(f"教師データが見つかりません: {csv_path}")

        self.use_ap_state = use_ap_state
        self.num_aps = num_aps

        xs_list = []
        ys_list = []

        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                n_cols = len(row)

                # CSV: measured_tp, measured_rtt, appIdx, [AP_tp, AP_rtt, AP_n]*N, label
                measured_tp  = float(row[0])
                measured_rtt = float(row[1])
                app_idx      = int(row[2])
                label        = int(row[-1])  # 最終列が常にラベル

                # 基本6次元特徴量 (実測 TP/RTT を正規化)
                tp_norm  = float(np.clip(measured_tp  / self.TP_MAX, 0.0, 1.0))
                rtt_norm = float(np.clip(measured_rtt / self.RTT_MAX, 0.0, 1.0))

                one_hot = np.zeros(APP_COUNT, dtype=np.float32)
                if 0 <= app_idx < APP_COUNT:
                    one_hot[app_idx] = 1.0

                base_features = np.concatenate([[tp_norm, rtt_norm], one_hot]).astype(np.float32)

                # AP状態特徴量（use_ap_state=True かつ CSV にAP状態列がある場合）
                if use_ap_state and n_cols > 4:
                    ap_cols_start = 3
                    ap_cols_end = n_cols - 1  # label 列を除く
                    ap_raw = [float(row[j]) for j in range(ap_cols_start, ap_cols_end)]
                    # 正規化: [tp, rtt, n] * num_aps
                    ap_norm = []
                    for a in range(num_aps):
                        idx = a * 3
                        if idx + 2 < len(ap_raw):
                            ap_norm.append(float(np.clip(ap_raw[idx]     / self.AP_TP_MAX,  0.0, 1.0)))
                            ap_norm.append(float(np.clip(ap_raw[idx + 1] / self.AP_RTT_MAX, 0.0, 1.0)))
                            ap_norm.append(float(np.clip(ap_raw[idx + 2] / self.AP_N_MAX,   0.0, 1.0)))
                    X_t = np.concatenate([base_features, np.array(ap_norm, dtype=np.float32)])
                else:
                    X_t = base_features

                xs_list.append(X_t)
                ys_list.append(label)

        self.X = torch.tensor(np.array(xs_list))
        self.y = torch.tensor(np.array(ys_list, dtype=np.int64))
        self.n_classes = int(self.y.max().item()) + 1

    def __len__(self) -> int:
        return len(self.X)

    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, torch.Tensor]:
        return self.X[idx], self.y[idx]

    @property
    def class_distribution(self) -> dict:
        counts = self.y.bincount(minlength=self.n_classes).tolist()
        return {f"AP_{i}": c for i, c in enumerate(counts)}


# ------------------------------------------------------------------ #
# IID 分割
# ------------------------------------------------------------------ #
def split_iid(dataset: Dataset, num_clients: int, seed: int = 42) -> List[Subset]:
    n     = len(dataset)
    sizes = [n // num_clients] * num_clients
    sizes[-1] += n - sum(sizes)
    generator = torch.Generator().manual_seed(seed)
    from torch.utils.data import random_split
    return list(random_split(dataset, sizes, generator=generator))

# ------------------------------------------------------------------ #
# non-IID 分割（ディリクレ分布）
# ------------------------------------------------------------------ #
def split_non_iid(
    dataset: Dataset,
    num_clients: int,
    alpha: float = 0.5,
    seed:  int   = 42,
) -> List[Subset]:
    rng    = np.random.default_rng(seed)
    labels = np.array([dataset[i][1].item() for i in range(len(dataset))])
    n_classes = int(labels.max()) + 1

    class_indices = [np.where(labels == c)[0] for c in range(n_classes)]
    client_indices: List[List[int]] = [[] for _ in range(num_clients)]

    for c_indices in class_indices:
        if len(c_indices) == 0: continue
        rng.shuffle(c_indices)
        proportions = rng.dirichlet(np.ones(num_clients) * alpha)
        proportions = (proportions * len(c_indices)).astype(int)
        diff = len(c_indices) - proportions.sum()
        proportions[np.argmax(proportions)] += diff

        start = 0
        for k, count in enumerate(proportions):
            client_indices[k].extend(c_indices[start : start + count].tolist())
            start += count

    return [Subset(dataset, idxs) for idxs in client_indices]
