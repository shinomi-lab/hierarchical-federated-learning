# Serverside_HFL/tests/test_trial_plan.py
import pytest
from scripts.trial_runner import plan

BASE = "settings: {seed: 1, terminals: [t1, t2]}\nconditions:\n"


def _write(tmp_path, body):
    p = tmp_path / "trials.yaml"
    p.write_text(BASE + body, encoding="utf-8")
    return p


def test_load_valid(tmp_path):
    s, cs = plan.load_trials(_write(tmp_path, "  - {name: a, mode: sync, baseline: none, num_cycles: 5, n: 2}\n"), delay_available=False)
    assert s["seed"] == 1 and cs[0].mode == "sync" and cs[0].n == 2 and cs[0].delay is None


@pytest.mark.parametrize("bad", [
    "  - {name: a, mode: fast, baseline: none, num_cycles: 5, n: 2}\n",      # 未知のモード
    "  - {name: a, mode: sync, baseline: x, num_cycles: 5, n: 2}\n",         # 未知のベースライン
    "  - {name: a, mode: sync, baseline: none, num_cycles: 0, n: 2}\n",      # サイクル数の範囲（SP3d: 1〜100）
    "  - {name: a, mode: sync, baseline: none, num_cycles: 5, n: 2}\n  - {name: a, mode: async, baseline: none, num_cycles: 5, n: 2}\n",  # 名前の重複
])
def test_reject_invalid(tmp_path, bad):
    with pytest.raises(plan.TrialsError):
        plan.load_trials(_write(tmp_path, bad), delay_available=False)


def test_delay_requires_targets_and_sudo(tmp_path):
    body = "  - {name: d, mode: async, baseline: none, num_cycles: 5, n: 2, delay: {A: 100, B: 0}}\n"
    with pytest.raises(plan.TrialsError, match="delay"):          # sudo -n が通らない
        plan.load_trials(_write(tmp_path, body), delay_available=False)
    with pytest.raises(plan.TrialsError, match="delay_targets"):  # settings に IP が無い
        plan.load_trials(_write(tmp_path, body), delay_available=True)
    p = tmp_path / "trials.yaml"
    p.write_text("settings: {seed: 1, terminals: [t1], delay_targets: {A: 192.168.11.0/24, B: 192.168.11.30},"
                 " delay_probes: {A: [192.168.11.1], B: [192.168.11.30]}}\nconditions:\n" + body, encoding="utf-8")
    assert plan.load_trials(p, delay_available=True)[1][0].delay == {"A": 100, "B": 0}


_DELAY_BODY = "  - {name: d, mode: async, baseline: none, num_cycles: 5, n: 2, delay: {A: 100, B: 0}}\n"


def _delay_yaml(tmp_path, probes):
    p = tmp_path / "trials.yaml"
    p.write_text("settings: {seed: 1, terminals: [t1], delay_targets: {A: 192.168.11.0/24, B: 192.168.11.30},"
                 f" delay_probes: {probes}}}\nconditions:\n" + _DELAY_BODY, encoding="utf-8")
    return p


def test_delay_probe_may_be_empty_for_b(tmp_path):
    # B（.30）は ping に応答しないので probe は空でよい（2026-10-22 の実機確認、runbook §0.2）
    p = _delay_yaml(tmp_path, "{A: [192.168.11.1], B: []}")
    assert plan.load_trials(p, delay_available=True)[1][0].delay == {"A": 100, "B": 0}


@pytest.mark.parametrize("probes", [
    "{A: [192.168.11.1]}",                  # B のキーが無い
    "{B: []}",                              # A のキーが無い
    "{A: [], B: []}",                       # 確認する相手が 1 つも無い
])
def test_delay_probes_still_validated(tmp_path, probes):
    with pytest.raises(plan.TrialsError, match="delay_probes"):
        plan.load_trials(_delay_yaml(tmp_path, probes), delay_available=True)


def _c(name, mode, n):
    return plan.Condition(name=name, mode=mode, baseline="none", num_cycles=5, delay=None, n=n)


CS = [_c("s1", "sync", 3), _c("s2", "sync", 3), _c("a1", "async", 3), _c("a2", "async", 3)]


def test_same_seed_same_order():
    a = [t.condition.name for t in plan.order_trials(CS, seed=7)]
    assert a == [t.condition.name for t in plan.order_trials(CS, seed=7)]
    assert len(a) == 12 and sorted(set(a)) == ["a1", "a2", "s1", "s2"]


def test_each_block_has_each_condition_once():
    ts = plan.order_trials(CS, seed=7)
    for b in range(3):
        assert sorted(t.condition.name for t in ts if t.block == b) == ["a1", "a2", "s1", "s2"]


def test_unequal_n():
    ts = plan.order_trials([_c("s1", "sync", 1), _c("a1", "async", 3)], seed=1)
    assert [t.condition.name for t in ts].count("a1") == 3 and [t.block for t in ts if t.condition.name == "s1"] == [0]


def test_run_tag_is_unique():
    ts = plan.order_trials(CS, seed=7)
    assert len({t.run_tag for t in ts}) == len(ts)
