"""AP の状態の集計（OpenSpec record-connected-ap, edge-ap-states-api）。標準ライブラリのみ。

中央サーバと、中央に届かないときのエッジの代替の両方が同じコードを使う。
- 接続数 n_connected: 端末ごとの最新の接続 AP のうち、保持時間（count_ttl_s、既定 600 秒）内のものを AP ごとに数える。
  足し引きするカウンタは持たない（同じ端末の通知で増え続けたり、初期値からずれたりしたため）。
- 実測 measured: その AP に繋がっている端末の新しい観測（保持時間 measured_ttl_s、既定 120 秒）の TP・RTT の中央値。
- 模擬値 tp_mbps / rtt_ms: 接続数から M/M/1（RTT）と Erlang-B（TP）で計算する。既定は 2026-10-22 のラボ実測（E4）に合わせる。
サーバの Python は 3.9 なので、`str | None` など 3.10 以降の構文は使わない。
"""
import statistics
import threading
import time
from typing import Callable, Dict, List, Optional

# 模擬値の既定。2026-10-22 ラボの実測。
#   TP: .11 で A/B を比較したアプリの計測の中央値（A 11.7・B 5.9 Mbps）。4 台同時では A 13.6〜21.8・B 12.6 とばらつく。
#   RTT: アプリが測る RTT（サーバ宛て。エッジのミドルウェア遅延を含む）。ping の最小値（A 3.0・B 6.7 ms）とは 20 倍違った。
#        同時に計測した実測は A 56.5〜60 ms（接続 3〜4 台）・B 78 ms（接続 1 台）。M/M/1 は接続数で RTT が増えるので、
#        「その接続数でこの実測になる」ように初期 RTT を逆算した（A: 1000/(1000/47-4)=57.6 ms、B: 1000/(1000/72-1)=77.6 ms）。
# 以前の既定（AP0 60 Mbps・80 ms、AP1 40 Mbps・100 ms）は TP が実機と 5〜7 倍離れていた。ラボで取り直したら、ここの数値だけを更新する。
# mu・n は Erlang-B のパラメータ（変えていない）。
AP_PARAMS = {
    "AP0": {"initial_rtt_ms": 47.0, "tp_init_mbps": 11.7, "mu": 3.0, "n": 5},
    "AP1": {"initial_rtt_ms": 72.0, "tp_init_mbps": 5.9, "mu": 4.0, "n": 8},
}
# 3 つ目以降の AP を設定で増やしたときの既定（実験では使わない）
_EXTRA_PARAMS = {"initial_rtt_ms": 200.0, "tp_init_mbps": 20.0, "mu": 6.0, "n": 12}

RTT_SATURATED_MS = 9999.0


def ap_ids_for(num_aps: int) -> List[str]:
    return ["AP%d" % i for i in range(max(0, int(num_aps)))]


def _erlang_b(lam: float, mu: float, n: int) -> float:
    """Erlang-B のブロック確率（安定な漸化式）。"""
    rho = lam / mu if mu > 0 else 0.0
    if rho <= 0:
        return 0.0
    inv_b = 1.0
    for k in range(1, n + 1):
        inv_b = 1.0 + inv_b * k / rho
    return 1.0 / inv_b


def model_state(ap_id: str, n_conn: int) -> dict:
    p = AP_PARAMS.get(ap_id, _EXTRA_PARAMS)
    mu_rtt = 1000.0 / p["initial_rtt_ms"] if p["initial_rtt_ms"] > 0 else 100.0
    lam = float(n_conn)
    rtt = 1000.0 / (mu_rtt - lam) if mu_rtt > lam else RTT_SATURATED_MS
    tp = p["tp_init_mbps"] * (1.0 - _erlang_b(lam, p["mu"], int(p["n"])))
    return {"tp_mbps": round(tp, 3), "rtt_ms": round(rtt, 3)}


class ApStateAggregator:
    def __init__(self, ap_ids: Optional[List[str]] = None, count_ttl_s: float = 600.0,
                 measured_ttl_s: float = 120.0, clock: Callable[[], float] = time.time) -> None:
        self.ap_ids = list(ap_ids) if ap_ids is not None else ap_ids_for(2)
        self.count_ttl_s = float(count_ttl_s)
        self.measured_ttl_s = float(measured_ttl_s)
        self.clock = clock
        # terminal_id -> {"ap": str, "ts": float, "tp": Optional[float], "rtt": Optional[float]}
        self._terminals = {}  # type: Dict[str, dict]
        # エッジの代替（local）は FastAPI のスレッドプールから同時に呼ばれる。辞書の反復中に追加されないようロックする
        self._lock = threading.RLock()

    def is_valid_ap(self, ap: str) -> bool:
        return ap in self.ap_ids

    def _check(self, ap: str) -> None:
        if not self.is_valid_ap(ap):
            raise ValueError("Unknown AP: %r" % (ap,))

    def connect(self, terminal_id: str, to_ap: str) -> None:
        """端末の接続 AP を更新する。AP が変わった端末の古い実測は持ち越さない。"""
        self._check(to_ap)
        with self._lock:
            self._terminals[terminal_id] = {"ap": to_ap, "ts": self.clock(), "tp": None, "rtt": None}

    def observe(self, terminal_id: str, ap: str, tp_mbps: Optional[float] = None,
                rtt_ms: Optional[float] = None) -> None:
        self._check(ap)
        with self._lock:
            self._terminals[terminal_id] = {"ap": ap, "ts": self.clock(), "tp": tp_mbps, "rtt": rtt_ms}

    def snapshot(self, now: Optional[float] = None) -> Dict[str, dict]:
        now = self.clock() if now is None else now
        with self._lock:
            terminals = list(self._terminals.values())
        out = {}
        for ap in self.ap_ids:
            mine = [t for t in terminals if t["ap"] == ap and now - t["ts"] <= self.count_ttl_s]
            fresh = [t for t in mine if now - t["ts"] <= self.measured_ttl_s and (t["tp"] is not None or t["rtt"] is not None)]
            state = model_state(ap, len(mine))
            state["n_connected"] = len(mine)
            state["measured"] = self._measured(fresh, now)
            out[ap] = state
        return out

    @staticmethod
    def _measured(fresh: List[dict], now: float) -> Optional[dict]:
        if not fresh:
            return None
        tps = [t["tp"] for t in fresh if t["tp"] is not None]
        rtts = [t["rtt"] for t in fresh if t["rtt"] is not None]
        return {
            "tp_mbps": statistics.median(tps) if tps else None,
            "rtt_ms": statistics.median(rtts) if rtts else None,
            "n": len(fresh),
            "age_s": round(now - max(t["ts"] for t in fresh), 3),
        }
