"""
sim/data_gen.py
---------------
AP 選択タスク用の合成データセット生成器。
旧実装（hungarian_main.py）および実機（DecisionEngine.kt）の意図に完全準拠。

特徴量 (6次元): 
  x[0]: needTP_norm   アプリの要求TP（正規化） [0, 1] 
  x[1]: needRTT_norm  アプリの要求RTT（正規化） [0, 1] 
  x[2]: app_browser  one-hot
  x[3]: app_video    one-hot
  x[4]: app_call     one-hot
  x[5]: app_other    one-hot

ラベル (2クラス): 
  y ∈ {0, 1} = 接続すべき最適な AP の ID
    0: AP_A (Wi-Fi 相当: 高TP, 高負荷時高RTT)
    1: AP_B (Cellular 相当: 低TP, 低RTT)

アプリ要件: 実機 ap_config.json と完全一致
  needTP : browser=5.0, video=2.0, call=10.0(未使用), other=1.0  [Mbps]
  needRTT: browser=100,  video=50,  call=200,          other=30   [ms]

正規化範囲: 
  TP_MAX  = 60 Mbps
  RTT_MAX = 2000 ms

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
# 2026-08-19 実機app.json/教師CSVに整合(方向A): index3 は other でなく 配信(streaming, RTT優先)。
# 旧定義(other/TP優先, video TP=2)は app.json・教師CSVと不一致だったため修正。
APP_TYPES = ["browser", "video", "call", "streaming"]   # streaming = 配信 (実機app.json 4番目)
APP_COUNT = 4

# 各アプリの QoS 要件 (実機 app.json と一致: browser TP5, video TP10, call RTT200, 配信 RTT100)
_TP_NEEDS  = [5.0, 10.0,  0.0,   0.0]   # Mbps: browser, video (call/配信 は RTT優先で未使用)
_RTT_NEEDS = [0.0,  0.0, 200.0, 100.0]  # ms:  call, 配信 (browser/video は TP優先で未使用)
_RTT_PRIOR = {2, 3}   # call, 配信(streaming) は RTT 優先

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
    ファイル形式: needTP, needRTT, appIdx, label
    """
    TP_MAX  = 60.0
    RTT_MAX = 2000.0

    def __init__(self, csv_path: str = None, n_samples: int = None, seed: int = 42) -> None:
        import csv
        import os
        
        # パスが指定されていない場合のデフォルト
        if csv_path is None:
            # リポジトリからの相対で解決する（CI など別の場所でも同じファイルを読む）
            csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                    "training_data_list", "default", "training_data.csv")
        
        if not os.path.exists(csv_path):
            raise FileNotFoundError(f"教師データが見つかりません: {csv_path}")

        xs_list = []
        ys_list = []
        
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row: continue
                # CSV: needTP, needRTT, appIdx, label
                need_tp  = float(row[0])
                need_rtt = float(row[1])
                app_idx  = int(row[2])
                label    = int(row[3])

                # 特徴量正規化
                tp_norm  = float(np.clip(need_tp / self.TP_MAX, 0.0, 1.0))
                rtt_norm = float(np.clip(1.0 - need_rtt / self.RTT_MAX, 0.0, 1.0))
                
                one_hot = np.zeros(APP_COUNT, dtype=np.float32)
                if 0 <= app_idx < APP_COUNT:
                    one_hot[app_idx] = 1.0
                
                X_t = np.concatenate([[tp_norm, rtt_norm], one_hot]).astype(np.float32)
                xs_list.append(X_t)
                ys_list.append(label)

        self.X = torch.tensor(np.array(xs_list))
        self.y = torch.tensor(np.array(ys_list, dtype=np.int64))
        self.n_classes = 4 # ネットワーク構造維持のため4クラス

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
