"""
sim/data_gen.py
---------------
AP 選択タスク用の合成データセット生成器。
実機 (TrainingViewModel.kt / LocalTrainer.kt) の入出力形式に完全準拠。

特徴量 (6次元): 実機 performLocalTrainingSuspend と同一
  x[0]: TP_norm   正規化スループット [0, 1]  (実機: TP Mbps)
  x[1]: RTT_norm  正規化往復遅延    [0, 1]  (実機: RTT ms、小さいほど良いので反転)
  x[2]: app_browser  one-hot
  x[3]: app_video    one-hot
  x[4]: app_call     one-hot
  x[5]: app_other    one-hot

ラベル (4クラス): 実機 outputSize = APP_CAT_COUNT = 4
  y ∈ {0, 1, 2, 3} = 実行中アプリの満足度ティア
    0: very low  (S < 0.25)   → 接続品質が要件を大きく下回る
    1: low       (0.25 ≤ S < 0.50) → 要件をある程度下回る
    2: medium    (0.50 ≤ S < 0.75) → 要件を概ね満たす
    3: high      (0.75 ≤ S ≤ 1.0) → 要件を十分満たす

  満足度: TerminalSatisfaction.calculateSatisfaction() と同一式
    TP優先 (browser, video, other): S = clip(TP / TP_need, 0, 1)
    RTT優先 (call):                 S = clip(RTT_need / RTT, 0, 1)

AP選択への活用:
  - ティア 0・1 (低満足) → 現在の AP は不適切 → 切替を検討
  - ティア 2・3 (高満足) → 現在の AP は適切  → 維持

アプリ要件: 実機 ap_config.json と完全一致
  needTP : browser=5.0, video=2.0, call=10.0(未使用), other=1.0  [Mbps]
  needRTT: browser=100,  video=50,  call=200,          other=30   [ms]

正規化範囲: 実機サーバー virtual_congestion.py の出力をカバーする範囲
  TP_MAX  = 60 Mbps  (AP_A tp_init=50Mbps を余裕を持ってカバー)
  RTT_MAX = 2000 ms  (AP_A 1端末接続時 RTT=1000ms をカバー)

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
APP_COUNT = 4

# 各アプリの QoS 要件（実機 ap_config.json と完全一致）
# ap_config.json: needTP={app0:5.0, app1:2.0, app2:10.0, app3:1.0}
#                 needRTT={app0:100, app1:50, app2:200, app3:30}
# app0=browser, app1=video, app2=call, app3=other
_TP_NEEDS  = [5.0,  2.0, 10.0,  1.0]   # Mbps: browser, video, call(未使用), other
_RTT_NEEDS = [100.0, 50.0, 200.0, 30.0]  # ms
_RTT_PRIOR = {2}   # call は RTT 優先（インデックス2）

# 満足度ティア境界
_TIER_BOUNDS = [0.0, 0.25, 0.50, 0.75, 1.01]   # 4 ティア: [0,0.25), [0.25,0.5), [0.5,0.75), [0.75,1.0]


def terminal_satisfaction(app_idx: int, tp: float, rtt: float) -> float:
    """
    実機 TerminalSatisfaction.calculateSatisfaction() と同一式。
    TP優先:  S = clip(TP / TP_need, ε, 1)
    RTT優先: S = clip(RTT_need / RTT, ε, 1)
    """
    eps = 1e-6
    if app_idx in _RTT_PRIOR:
        raw = _RTT_NEEDS[app_idx] / max(rtt, eps)
    else:
        raw = tp / max(_TP_NEEDS[app_idx], eps)
    return float(min(1.0, max(eps, raw)))


def satisfaction_to_tier(s: float) -> int:
    """満足度スカラー → 4クラスティア"""
    if s < 0.25:
        return 0
    if s < 0.50:
        return 1
    if s < 0.75:
        return 2
    return 3


class APSelectionDataset(Dataset):
    """
    AP 選択タスクの合成データセット。

    実機フォーマット:
      入力 (6次元): [TP_norm, RTT_norm, app_one_hot(4D)]
      出力 (4クラス): 実行中アプリの接続品質ティア (0=very_low … 3=high)

    モデルが学習すること:
      「このTP/RTT環境は、実行中のアプリにとって何ティアの満足度か」を分類する。
      → ティア0・1なら AP切替を検討、ティア2・3なら維持

    ラベル分布:
      各ティアが均等に約25%になるよう、
      満足度ターゲット S → TP/RTT を逆算してデータを生成する。
      これにより trivial 解（常にクラス0を予測）が生まれない。

    TP/RTT の正規化 (推論フェーズとの整合):
      TP:  最大 20 Mbps を想定。 TP_norm = TP / 20.0 ∈ [0, 1]
      RTT: 最大 500 ms を想定。 RTT_norm = 1 - RTT / 500.0 ∈ [0, 1]  (小→良)
    """

    # 実機サーバー virtual_congestion.py の出力をカバーする正規化基準
    # AP_A: tp_init=50Mbps → TP_MAX=60 で余裕確保
    # AP_A 1端末接続: RTT=1000ms → RTT_MAX=2000 でカバー
    TP_MAX  = 60.0    # Mbps
    RTT_MAX = 2000.0  # ms

    def __init__(self, n_samples: int = 3000, seed: int = 42) -> None:
        rng = np.random.default_rng(seed)

        n_per_tier = n_samples // 4
        remainder  = n_samples - n_per_tier * 4

        xs_list: list[np.ndarray] = []
        ys_list: list[int]        = []

        for tier in range(4):
            # このティアのサンプル数（最後のティアに余りを追加）
            n_t = n_per_tier + (remainder if tier == 3 else 0)

            # ─ 満足度を tier の区間から一様サンプリング ─────────
            s_lo = _TIER_BOUNDS[tier]
            s_hi = _TIER_BOUNDS[tier + 1]
            # 下限を少し上げてゼロ除算を避ける
            S = rng.uniform(max(s_lo, 1e-3), min(s_hi - 1e-4, 1.0), n_t)

            # ─ アプリカテゴリを一様サンプリング ───────────────
            app_idx = rng.integers(0, APP_COUNT, n_t)

            # ─ S と app から TP/RTT を逆算 ─────────────────────
            tp_raw  = np.zeros(n_t, dtype=np.float32)
            rtt_raw = np.zeros(n_t, dtype=np.float32)

            for i in range(n_t):
                ai  = int(app_idx[i])
                s   = float(S[i])
                noise_scale = 0.05   # 5% の揺らぎを加える（現実的なばらつき）

                if ai in _RTT_PRIOR:
                    # call: S = RTT_need / RTT  →  RTT = RTT_need / S
                    rtt_target = _RTT_NEEDS[ai] / s
                    rtt_noise  = rng.normal(0, noise_scale * rtt_target)
                    rtt_raw[i] = float(np.clip(rtt_target + rtt_noise, 1.0, self.RTT_MAX))
                    tp_raw[i]  = float(rng.uniform(0.1, self.TP_MAX))  # TP は自由

                    # ティア3: RTT << RTT_need の「飽和領域」を追加
                    # AP_B (RTT≈200-500ms) など低RTT環境を訓練分布に含める
                    if tier == 3 and rng.random() < 0.4:
                        rtt_raw[i] = float(rng.uniform(1.0, _RTT_NEEDS[ai] * 0.5))
                else:
                    # browser/video/other: S = TP / TP_need  →  TP = S × TP_need
                    tp_target = s * _TP_NEEDS[ai]
                    tp_noise  = rng.normal(0, noise_scale * max(tp_target, 0.1))
                    tp_raw[i]  = float(np.clip(tp_target + tp_noise, 0.0, self.TP_MAX))
                    rtt_raw[i] = float(rng.uniform(1.0, self.RTT_MAX))  # RTT は自由

                    # ティア3: TP >> TP_need の「飽和領域」を追加
                    # AP_A (tp_init=50Mbps) のような高TP環境を訓練分布に含める
                    if tier == 3 and rng.random() < 0.4:
                        tp_raw[i] = float(rng.uniform(_TP_NEEDS[ai], self.TP_MAX))

            # ─ 特徴量正規化 ───────────────────────────────────
            tp_norm  = np.clip(tp_raw  / self.TP_MAX,              0.0, 1.0)
            rtt_norm = np.clip(1.0 - rtt_raw / self.RTT_MAX,       0.0, 1.0)

            # ─ app one-hot ─────────────────────────────────────
            one_hot = np.zeros((n_t, APP_COUNT), dtype=np.float32)
            one_hot[np.arange(n_t), app_idx] = 1.0

            # ─ 特徴量行列 ─────────────────────────────────────
            X_t = np.column_stack([tp_norm, rtt_norm, one_hot]).astype(np.float32)
            xs_list.append(X_t)
            ys_list.extend([tier] * n_t)

        # シャッフル（ティアが連続しないように）
        X_all = np.concatenate(xs_list, axis=0)
        y_all = np.array(ys_list, dtype=np.int64)
        perm  = rng.permutation(len(y_all))
        X_all = X_all[perm]
        y_all = y_all[perm]

        self.X = torch.tensor(X_all)
        self.y = torch.tensor(y_all)
        self.n_classes = APP_COUNT

    def __len__(self) -> int:
        return len(self.X)

    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, torch.Tensor]:
        return self.X[idx], self.y[idx]

    @property
    def class_distribution(self) -> dict:
        counts = self.y.bincount().tolist()
        return {f"tier{i}_{APP_TYPES[i]}": c for i, c in enumerate(counts)}


# ------------------------------------------------------------------ #
# IID 分割
# ------------------------------------------------------------------ #
def split_iid(dataset: Dataset, num_clients: int, seed: int = 42) -> List[Subset]:
    """データセットを均等に IID 分割する"""
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
    """
    ディリクレ分布を用いて non-IID にデータを分割する。
    alpha が小さいほどラベル偏りが強くなる。
    """
    rng    = np.random.default_rng(seed)
    labels = np.array([dataset[i][1].item() for i in range(len(dataset))])
    n_classes = int(labels.max()) + 1

    class_indices = [np.where(labels == c)[0] for c in range(n_classes)]
    client_indices: List[List[int]] = [[] for _ in range(num_clients)]

    for c_indices in class_indices:
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
