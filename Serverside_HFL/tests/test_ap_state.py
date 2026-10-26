"""shared.ap_state の試験（OpenSpec record-connected-ap 1.1・1.5, edge-ap-states-api）。

接続数は「端末ごとの最新の接続 AP」から数え、実測（measured）は保持時間内の中央値。時計は差し替えて決定論的にする。
模擬値の期待値は、コードの再帰式とは別に Erlang-B を定義どおり (A^n/n!) / Σ(A^k/k!) で手計算したもの:
  AP0 接続 0: rtt = 1000/(1000/47.0 - 0) = 47.0 ms,  tp = 11.7 Mbps
  AP1 接続 0: rtt = 72.0 ms,                          tp = 5.9 Mbps
  AP0 接続 4 (mu=3, n=5): A=4/3 → B = (A^5/120) / Σ_{k=0..5}(A^k/k!) = 0.1... を下の _erlang_b_ref で計算
"""
import math

import pytest

from shared.ap_state import ApStateAggregator, ap_ids_for


def _erlang_b_ref(a, n):
    num = a ** n / math.factorial(n)
    return num / sum(a ** k / math.factorial(k) for k in range(n + 1))


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def agg():
    c = Clock()
    a = ApStateAggregator(ap_ids=["AP0", "AP1"], count_ttl_s=600, measured_ttl_s=120, clock=c)
    a.clock = c
    return a


def test_ap_ids_follow_ap_num_max():
    assert ap_ids_for(2) == ["AP0", "AP1"]
    assert ap_ids_for(3) == ["AP0", "AP1", "AP2"]


def test_startup_all_zero_and_only_configured_aps(agg):
    snap = agg.snapshot()
    assert set(snap) == {"AP0", "AP1"}  # AP2 は返さない
    for ap, s in snap.items():
        assert s["n_connected"] == 0, ap
        assert s["measured"] is None, ap
        assert set(s) >= {"tp_mbps", "rtt_ms", "n_connected", "measured"}


def test_defaults_match_lab_measurements_when_idle(agg):
    snap = agg.snapshot()
    assert snap["AP0"]["tp_mbps"] == pytest.approx(11.7, abs=0.01)
    assert snap["AP0"]["rtt_ms"] == pytest.approx(47.0, abs=0.01)
    assert snap["AP1"]["tp_mbps"] == pytest.approx(5.9, abs=0.01)
    assert snap["AP1"]["rtt_ms"] == pytest.approx(72.0, abs=0.01)


def test_model_values_degrade_with_connections_and_match_hand_computation(agg):
    for i in range(4):
        agg.connect(f"terminal-0{i + 1}", "AP0")
    s = agg.snapshot()["AP0"]
    assert s["n_connected"] == 4
    b = _erlang_b_ref(4 / 3.0, 5)  # lam=4, mu=3, n=5 → A=4/3
    assert s["tp_mbps"] == pytest.approx(11.7 * (1 - b), abs=0.002)
    assert s["rtt_ms"] == pytest.approx(1000.0 / (1000.0 / 47.0 - 4), abs=0.002)
    idle = ApStateAggregator(ap_ids=["AP0", "AP1"]).snapshot()["AP0"]
    assert s["rtt_ms"] >= idle["rtt_ms"] and s["tp_mbps"] <= idle["tp_mbps"]


def test_repeated_connect_of_same_terminal_counts_once(agg):
    for _ in range(24):
        agg.connect("terminal-01", "AP0")
    assert agg.snapshot()["AP0"]["n_connected"] == 1


def test_switch_moves_the_count(agg):
    agg.connect("terminal-01", "AP0")
    agg.connect("terminal-02", "AP0")
    agg.connect("terminal-01", "AP1")
    s = agg.snapshot()
    assert s["AP0"]["n_connected"] == 1 and s["AP1"]["n_connected"] == 1


def test_terminals_expire_after_ttl(agg):
    agg.connect("terminal-01", "AP0")
    agg.clock.t += 601
    assert agg.snapshot()["AP0"]["n_connected"] == 0
    agg.observe("terminal-01", "AP0", tp_mbps=10.0, rtt_ms=4.0)
    assert agg.snapshot()["AP0"]["n_connected"] == 1


@pytest.mark.parametrize("bad", ["AP9", "AP2", "", "apA"])
def test_unknown_ap_is_rejected_and_nothing_changes(agg, bad):
    with pytest.raises(ValueError):
        agg.connect("terminal-01", bad)
    with pytest.raises(ValueError):
        agg.observe("terminal-01", bad, tp_mbps=1.0, rtt_ms=1.0)
    assert all(s["n_connected"] == 0 for s in agg.snapshot().values())


def test_observe_feeds_measured_and_count(agg):
    agg.observe("terminal-01", "AP1", tp_mbps=6.0, rtt_ms=40.0)
    s = agg.snapshot()["AP1"]
    assert s["n_connected"] == 1
    assert s["measured"] == {"tp_mbps": 6.0, "rtt_ms": 40.0, "n": 1, "age_s": 0.0}


def test_measured_is_median_across_terminals(agg):
    for tid, tp in (("t1", 10.0), ("t2", 11.0), ("t3", 30.0)):
        agg.observe(tid, "AP0", tp_mbps=tp, rtt_ms=5.0)
    m = agg.snapshot()["AP0"]["measured"]
    assert m["tp_mbps"] == 11.0 and m["n"] == 3


def test_stale_measurement_returns_to_null_but_count_remains(agg):
    agg.observe("terminal-01", "AP1", tp_mbps=6.0, rtt_ms=40.0)
    agg.clock.t += 121
    s = agg.snapshot()["AP1"]
    assert s["measured"] is None  # 保持時間 120 秒を過ぎた
    assert s["n_connected"] == 1  # 接続数の保持時間(600 秒)内


def test_age_is_since_latest_observation(agg):
    agg.observe("t1", "AP0", tp_mbps=10.0, rtt_ms=5.0)
    agg.clock.t += 30
    agg.observe("t2", "AP0", tp_mbps=12.0, rtt_ms=5.0)
    agg.clock.t += 10
    assert agg.snapshot()["AP0"]["measured"]["age_s"] == 10.0


def test_connect_clears_the_old_measurement_of_that_terminal(agg):
    agg.observe("terminal-01", "AP0", tp_mbps=11.0, rtt_ms=3.0)
    agg.connect("terminal-01", "AP1")  # 切り替えた端末の古い AP の実測を新しい AP に持ち越さない
    s = agg.snapshot()
    assert s["AP0"]["measured"] is None and s["AP1"]["measured"] is None
    assert s["AP1"]["n_connected"] == 1


def test_partial_observation_tp_only(agg):
    agg.observe("terminal-01", "AP0", tp_mbps=9.0)
    m = agg.snapshot()["AP0"]["measured"]
    assert m["tp_mbps"] == 9.0 and m["rtt_ms"] is None and m["n"] == 1


def test_saturated_model_rtt_is_capped(agg):
    for i in range(400):
        agg.connect(f"t{i}", "AP0")
    assert agg.snapshot()["AP0"]["rtt_ms"] == 9999.0


def test_model_reproduces_the_measured_app_rtt_at_the_measured_load(agg):
    """既定の初期 RTT は、同時計測の実測（A 約 58 ms @3〜4 台、B 約 78 ms @1 台）を、その接続数で再現するように決めた。"""
    for i in range(4):
        agg.connect("a%d" % i, "AP0")
    agg.connect("b0", "AP1")
    s = agg.snapshot()
    assert s["AP0"]["rtt_ms"] == pytest.approx(57.6, abs=1.0)
    assert s["AP1"]["rtt_ms"] == pytest.approx(77.6, abs=1.0)


def test_aggregator_is_thread_safe_under_concurrent_observe_and_snapshot():
    """エッジの代替（local）は FastAPI のスレッドプールから、観測の追加と snapshot が同時に呼ばれる。辞書の反復中に追加されて落ちない（レビュー指摘）。"""
    import threading

    a = ApStateAggregator(ap_ids=["AP0", "AP1"])
    errors = []

    def writer(k):
        try:
            for i in range(3000):
                a.observe("t%d-%d" % (k, i), "AP%d" % (i % 2), tp_mbps=5.0, rtt_ms=10.0)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def reader():
        try:
            for _ in range(600):
                a.snapshot()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=writer, args=(k,)) for k in range(4)] + [threading.Thread(target=reader) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []


def test_is_valid_ap_is_public():
    a = ApStateAggregator(ap_ids=["AP0", "AP1"])
    assert a.is_valid_ap("AP1") and not a.is_valid_ap("AP2")
