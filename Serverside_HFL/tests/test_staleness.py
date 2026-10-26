import asyncio

from central_server.endpoints import edge_update
from edge_server.endpoints import aggregation
from edge_server.endpoints import terminal_update  # noqa: F401  import 時に asyncio.Lock() を作るので先に読む
from edge_server.state import current_edge_state


def _run_async(coro):
    """asyncio.run は終了時に既定ループを None にし、Python 3.9 では後続の import 時の asyncio.Lock() が失敗する。
    既定ループに触れない専用ループで実行する。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---- A-4: 中央の staleness は中央ラウンド同士の差 ----

def test_staleness_uses_central_rounds():
    assert edge_update.compute_central_staleness(9, base_central_round=7, edge_round_id=40, round_=40) == (2, "base_central_round")


def test_staleness_legacy_fallback_without_base():
    assert edge_update.compute_central_staleness(9, base_central_round=None, edge_round_id=8, round_=8) == (1, "legacy")


def test_edge_form_carries_base_central_round(monkeypatch):
    # 2.3: フォームは引数の値（集約時点の値）を使い、現在の状態は読まない
    monkeypatch.setattr(current_edge_state, "base_central_round", 8, raising=False)
    f = aggregation.central_form_fields(round_id=40, base_central_round=7)
    assert f["base_central_round"] == "7" and f["edge_round_id"] == "40"


# ---- 2.3 (R2): 送るのは集約した時点の base_central_round ----

def test_form_uses_base_at_aggregation_time():
    assert aggregation.central_form_fields(40, base_central_round=7)["base_central_round"] == "7"
    assert "base_central_round" not in aggregation.central_form_fields(40, base_central_round=None)


def test_pending_meta_keeps_base_central_round_at_aggregation_time(monkeypatch, tmp_path):
    import asyncio
    import torch
    from edge_server import state as edge_state

    buckets = {}
    monkeypatch.setattr(edge_state, "terminal_state_dicts", buckets)
    monkeypatch.setattr(aggregation, "terminal_state_dicts", buckets)
    monkeypatch.setattr(aggregation, "AGGREGATION_CACHE_DIR", tmp_path)
    monkeypatch.setattr(aggregation, "_pending_index", {})
    monkeypatch.setattr(current_edge_state, "base_central_round", 7, raising=False)

    sd = {"w": torch.ones(2)}
    edge_state.append_update(40, UpdateRecord(terminal_id="t1", state_dict=sd, n_samples=1, sig="a", path="p"))
    resp = _run_async(aggregation.aggregate_and_push(round_id=40, model_id=None, auto_send=False, interactive=False))
    # 集約のあと、送信する前に新しいグローバルモデル（中央ラウンド 8）を受け取る
    current_edge_state.base_central_round = 8

    meta = aggregation._pending_index[resp["filename"]]
    assert meta["base_central_round"] == 7
    assert aggregation.central_form_fields(40, meta.get("base_central_round"))["base_central_round"] == "7"


def test_post_to_central_sends_given_base_not_current_state(monkeypatch, tmp_path):
    import asyncio
    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, files=None, data=None):
            sent.update(data)
            return _Resp()

    monkeypatch.setattr(aggregation.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(current_edge_state, "base_central_round", 8, raising=False)
    f = tmp_path / "agg.pt"
    f.write_bytes(b"x")
    _run_async(aggregation._post_to_central_from_path(
        f, edge_id="e1", round_id=40, model_id=None, num_clients=1, sum_n_samples=1, base_central_round=7))
    assert sent["base_central_round"] == "7"


# ---- 2.3 (R4): 集約に失敗したモデルの配布にラウンドを付けない ----

def test_push_kwargs_has_central_round_only_when_aggregated():
    ok = edge_update.push_kwargs({"model_bytes": b"m", "config_bytes": b"c"}, round_id=8)
    assert ok == {"model_bytes": b"m", "config_bytes": b"c", "central_round": 9}
    failed = edge_update.push_kwargs(None, round_id=8)
    assert "central_round" not in failed
    assert failed["model_bytes"] is None and failed["config_bytes"] is None


def test_background_aggregate_failure_pushes_without_central_round(monkeypatch):
    import asyncio
    from central_server.endpoints import edge_management as em

    calls = []

    async def _fake_send(**kwargs):
        calls.append(kwargs)

    def _fail(_updates):
        raise ValueError("shape mismatch")

    monkeypatch.setattr(em, "async_send_model_and_app", _fake_send)
    monkeypatch.setattr(edge_update, "fedavg_multiple_edges", _fail)

    async def _run():
        await edge_update._background_aggregate(8, {}, None)
        for _ in range(5):
            await asyncio.sleep(0)

    _run_async(_run())
    assert len(calls) == 1
    assert "central_round" not in calls[0]


# ---- E-4: ラウンドの staleness 統計はそのラウンドの更新だけから ----

from edge_server.state import UpdateRecord


def _rec(tid, s):
    return UpdateRecord(terminal_id=tid, state_dict=None, n_samples=1, sig="x", path="p", staleness=s)


def test_update_record_default_staleness_is_zero():
    assert UpdateRecord(terminal_id="t", state_dict=None, n_samples=1, sig="x", path="p").staleness == 0


def test_round_stats_use_only_this_rounds_records(monkeypatch):
    from edge_server.endpoints import terminal_update
    monkeypatch.setitem(terminal_update.terminal_staleness_accum, "t1", [5, 5, 5])   # 過去の大きな staleness
    st = aggregation.round_staleness_stats([_rec("t1", 0), _rec("t2", 0)])
    assert st == {"staleness_mean": 0.0, "staleness_max": 0, "staleness_p95": 0}


def test_round_stats_empty():
    assert aggregation.round_staleness_stats([]) == {"staleness_mean": None, "staleness_max": None, "staleness_p95": None}


def test_round_stats_values_and_extra_percentiles():
    recs = [_rec("t1", 0), _rec("t2", 1), _rec("t3", 2), _rec("t4", 3)]
    st = aggregation.round_staleness_stats(recs, percentiles=(50, 75, 95))
    assert st == {"staleness_mean": 1.5, "staleness_max": 3,
                  "staleness_p50": 1, "staleness_p75": 2, "staleness_p95": 2}


# ---- A-5: 集約中に届いた更新は次のラウンドへ持ち越す ----

def _isolated_buckets(monkeypatch):
    from edge_server import state as edge_state
    buckets = {}
    monkeypatch.setattr(edge_state, "terminal_state_dicts", buckets)
    monkeypatch.setattr(aggregation, "terminal_state_dicts", buckets)
    return edge_state


def test_updates_arriving_during_aggregation_are_carried_over(monkeypatch):
    import asyncio
    edge_state = _isolated_buckets(monkeypatch)
    r = 40
    edge_state.append_update(r, _rec("t1", 0))
    # 集約のスナップショット（aggregate_and_send_to_central_server と同じく取り出してから空にする）
    snapshot = list(edge_state.terminal_state_dicts.get(r, []))
    edge_state.clear_round(r)
    assert len(snapshot) == 1
    # スナップショット後・ラウンド進行前に届いた更新
    edge_state.append_update(r, _rec("t2", 1))

    carried = _run_async(aggregation.carry_over_bucket(r, r + 1))

    assert carried == 1
    assert edge_state.bucket_count(r + 1) == 1
    assert edge_state.bucket_count(r) == 0
    assert edge_state.terminal_state_dicts[r + 1][0].terminal_id == "t2"


def test_carry_over_without_leftovers_is_noop(monkeypatch):
    import asyncio
    edge_state = _isolated_buckets(monkeypatch)
    assert _run_async(aggregation.carry_over_bucket(7, 8)) == 0
    assert edge_state.bucket_count(8) == 0
