"""完了ステップ数の時間モデル（学習を走らせない）。

端末の所要ステップは async_runner と同じ式 max(1, round(N(mean, std))) で引く。
完了 = 全端末が C サイクルを終えるまで。
  Async: 端末ごとの C サイクル合計の最大（各端末は待たずに進む）
  Sync : サイクルごとの全端末最大の合計（毎サイクル最も遅い端末を待つ）
同じ乱数列から両方式を算出する。
"""
from __future__ import annotations

import numpy as np


def durations(seed: int, num_terminals: int, cycles: int, mean: float, std: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    d = rng.normal(mean, std, size=(num_terminals, cycles))
    return np.maximum(1, np.rint(d)).astype(int)


def completion_steps(d: np.ndarray) -> tuple[int, int]:
    """(async_steps, sync_steps)"""
    async_steps = int(d.sum(axis=1).max())
    sync_steps = int(d.max(axis=0).sum())
    return async_steps, sync_steps


def simulate(seed: int, num_terminals: int = 4, cycles: int = 30, mean: float = 1.0, std: float = 0.3):
    return completion_steps(durations(seed, num_terminals, cycles, mean, std))
