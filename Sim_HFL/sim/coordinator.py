"""中央協調器 (CTDE フェーズ1: control-plane 協調)。

各APの混雑応答(congestion_fn)から、全体の調和平均満足度が最大になる
端末→AP の joint 割当を計算する。反応型(独立greedy)が herding で振動し、
確率型(no-regret)が worst-case 敏感な HM を潰すのに対し、中央が joint 割当を
一括で決めることで oracle 近傍の安定な割当を得る(`analysis/coord_sim.py` で実証)。

満足度式は `terminal_satisfaction`(= 実機 TerminalSatisfaction)を共有。
"""
from __future__ import annotations

import itertools
from typing import Callable

from .data_gen import terminal_satisfaction

_EPS = 1e-6


def _harmonic_mean(vals: list[float]) -> float:
    if not vals:
        return 0.0
    return len(vals) / sum(1.0 / max(v, _EPS) for v in vals)


def central_coordinator(
    terminal_apps: list[int],
    num_aps: int,
    congestion_fn: Callable[[int, int], tuple[float, float]],
) -> list[int]:
    """調和平均満足度を最大化する端末→AP割当を返す。

    Parameters
    ----------
    terminal_apps : 端末ごとの app_idx
    num_aps       : AP 数
    congestion_fn : (n_connected, ap_idx) -> (tp, rtt) 決定論的な混雑予測。
                    候補割当ごとに接続台数が変わる混雑フィードバックを反映するため、
                    固定の実測値でなく「台数→(tp,rtt)」の関数を受け取る。

    Returns
    -------
    端末ごとの割当 AP index (len == len(terminal_apps))
    """
    N = len(terminal_apps)
    if N == 0:
        return []
    if num_aps == 2:
        return _coordinate_two_aps(terminal_apps, congestion_fn)

    # 一般 (num_aps > 2): 全列挙。N が小さい前提(実験は 4〜十数台)。
    best_assign: list[int] | None = None
    best_val = -1.0
    for assign in itertools.product(range(num_aps), repeat=N):
        counts = [0] * num_aps
        for a in assign:
            counts[a] += 1
        cond = [congestion_fn(counts[k], k) for k in range(num_aps)]
        sats = [
            terminal_satisfaction(terminal_apps[i], cond[assign[i]][0], cond[assign[i]][1])
            for i in range(N)
        ]
        v = _harmonic_mean(sats)
        if v > best_val:
            best_val, best_assign = v, list(assign)
    return best_assign if best_assign is not None else [0] * N


def _coordinate_two_aps(
    terminal_apps: list[int],
    congestion_fn: Callable[[int, int], tuple[float, float]],
) -> list[int]:
    """2AP 専用の高速版: 同一アプリ端末は交換可能なので、
    アプリ別に「AP0 に何台置くか(k_a)」の count 空間 (Π(c_a+1)) を列挙する。
    2^N ではなく数百通りに収まる。
    """
    N = len(terminal_apps)
    app_ids = sorted(set(terminal_apps))
    cnt = {a: terminal_apps.count(a) for a in app_ids}
    ranges = [range(cnt[a] + 1) for a in app_ids]

    best_kmap: dict[int, int] | None = None
    best_val = -1.0
    for kv in itertools.product(*ranges):
        kmap = dict(zip(app_ids, kv))
        n0 = sum(kmap.values())
        n1 = N - n0
        tp0, rtt0 = congestion_fn(n0, 0)
        tp1, rtt1 = congestion_fn(n1, 1)
        sats: list[float] = []
        for a in app_ids:
            s0 = terminal_satisfaction(a, tp0, rtt0)
            s1 = terminal_satisfaction(a, tp1, rtt1)
            sats += [s0] * kmap[a] + [s1] * (cnt[a] - kmap[a])
        v = _harmonic_mean(sats)
        if v > best_val:
            best_val, best_kmap = v, kmap

    if best_kmap is None:
        return [0] * N

    # count 割当 → 端末別割当 (各アプリの先頭 k 台を AP0, 残りを AP1)
    assign = [0] * N
    placed = {a: 0 for a in app_ids}
    for i, a in enumerate(terminal_apps):
        if placed[a] < best_kmap[a]:
            assign[i] = 0
            placed[a] += 1
        else:
            assign[i] = 1
    return assign
