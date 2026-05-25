"""
sim/network_model.py
--------------------
待ち行列理論に基づく AP ネットワーク条件シミュレーション。

実機サーバー edge_server/endpoints/virtual_congestion.py と完全同一の
M/M/1 + Erlang-B モデルを実装する。

■ RTT: M/M/1 モデル（サーバーと同一式）
  virtual_rtt = 1 / (μ − λ) × 1000  [ms]   λ = 接続端末数
  delta       = max(0, virtual_rtt − initial_rtt_ms)
  rtt_report  = initial_rtt_ms + delta
              = max(initial_rtt_ms, virtual_rtt)
  ※ λ ≥ μ の場合は飽和値 10000 ms

■ TP: Erlang-B モデル（サーバーと同一式）
  A       = λ / μ           (提供トラフィック Erlang)
  P_loss  = erlang_b(n, A)
  TP      = tp_init × (1 − P_loss)

実機 virtual_congestion.py のパラメータ（ = 実験で注入している遅延設定）:
  AP_A (WiFi):     initial_rtt=20ms, tp_init=50Mbps, mu=2.0, n=2
  AP_B (Cellular): initial_rtt=80ms, tp_init=20Mbps, mu=5.0, n=10
"""
from __future__ import annotations

import numpy as np


def erlang_b(n: int, A: float) -> float:
    """Erlang-B 閉塞確率 B(n, A) を安定漸化式で計算する。

    virtual_congestion.py の erlang_b() と完全同一実装。

    Parameters
    ----------
    n : チャネル数
    A : 提供トラフィック (Erlang)  A = λ / μ

    Returns
    -------
    閉塞確率 ∈ [0, 1]
    """
    if n <= 0:
        return 1.0
    if A <= 0.0:
        return 0.0
    B = 1.0
    for k in range(1, n + 1):
        B = (A * B) / (k + A * B)
    return float(B)


def mm1_rtt(n_connected: int, mu: float, initial_rtt_ms: float) -> float:
    """M/M/1 モデルで RTT を計算する（サーバー virtual_congestion.py と完全同一式）。

    virtual_rtt = 1 / (μ − λ) × 1000  [ms]
    delta       = max(0, virtual_rtt − initial_rtt_ms)
    rtt_report  = initial_rtt_ms + delta

    Parameters
    ----------
    n_connected   : AP に現在接続している端末数 (= λ)
    mu            : AP のサービス率 (= μ)。λ < μ で安定。
    initial_rtt_ms: 無負荷時の物理 RTT (ms) ← virtual_congestion.py の initial_rtt_ms

    Returns
    -------
    RTT (ms)。λ ≥ μ の場合は飽和値 10000 ms。
    """
    lam = float(n_connected)
    if lam >= mu:
        return 10000.0
    virtual_rtt = (1.0 / (mu - lam)) * 1000.0
    delta = max(0.0, virtual_rtt - initial_rtt_ms)
    return min(10000.0, initial_rtt_ms + delta)


def ap_conditions(
    n_connected:   int,
    mu_rtt:        float,
    base_rtt_ms:   float,
    n_channels:    int,
    tp_init_mbps:  float,
    rng:           np.random.Generator,
    rtt_noise_std: float = 5.0,
    tp_noise_std:  float = 1.0,
) -> tuple[float, float]:
    """サーバー virtual_congestion.py と同一モデルで AP の TP・RTT を計算する。

    Parameters
    ----------
    n_connected   : この AP に接続中の端末数 (λ)
    mu_rtt        : AP のサービス率 (μ) ← virtual_congestion.py の mu
    base_rtt_ms   : 無負荷時の物理 RTT (ms) ← virtual_congestion.py の initial_rtt_ms
    n_channels    : Erlang-B チャネル数 (n) ← virtual_congestion.py の n
    tp_init_mbps  : 無負荷時 TP (Mbps) ← virtual_congestion.py の tp_init_mbps
    rng           : 乱数ジェネレーター（再現性のため外部から受け取る）
    rtt_noise_std : RTT ガウスノイズ標準偏差 (ms)
    tp_noise_std  : TP ガウスノイズ標準偏差 (Mbps)

    Returns
    -------
    (tp_mbps, rtt_ms)
    """
    # ── RTT: M/M/1（サーバー式）─────────────────────────────────────
    rtt_base = mm1_rtt(n_connected, mu_rtt, base_rtt_ms)
    rtt_ms   = max(1.0, rtt_base + float(rng.normal(0.0, rtt_noise_std)))

    # ── TP: Erlang-B（サーバー式: offered_A = λ / μ）──────────────
    offered_A = float(n_connected) / mu_rtt if mu_rtt > 0 else 0.0
    p_loss    = erlang_b(n_channels, offered_A)
    tp_base   = tp_init_mbps * (1.0 - p_loss)
    tp_mbps   = max(0.1, tp_base + float(rng.normal(0.0, tp_noise_std)))

    return tp_mbps, rtt_ms


def congestion_summary(
    n_on_ap:   list[int],
    ap_params: list[dict],
) -> list[dict]:
    """各 AP の混雑状態サマリーを返す（ログ・表示用）。"""
    result = []
    for ap_idx, (n, params) in enumerate(zip(n_on_ap, ap_params)):
        mu     = params.get("mu_rtt", 1.0)
        A      = float(n) / mu if mu > 0 else 0.0
        p_loss = erlang_b(params.get("n_channels", 1), A)
        rtt    = mm1_rtt(n, mu, params.get("base_rtt_ms", 0.0))
        result.append({
            "ap":          f"AP_{ap_idx}",
            "n_connected": n,
            "offered_A":   round(A,      3),
            "p_loss_pct":  round(p_loss * 100, 2),
            "rtt_ms":      round(rtt,    1),
        })
    return result
