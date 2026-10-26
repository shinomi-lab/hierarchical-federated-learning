"""条件表 trials.yaml の読み込み・検証と、試行の展開・実行順（純関数）。"""
import copy
import random
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

MODES = ("sync", "async")
BASELINES = ("none", "fixed", "random", "greedy")  # experiment_control.MODES と同じ
NUM_CYCLES_MIN, NUM_CYCLES_MAX = 1, 100  # SP3d
DELAY_KEYS = ("A", "B")

# 既定は過去の実機（6/18）に準拠（2026-10-15 利用者決定）。start.py の既定（エッジ [2,1]）には頼らない
DEFAULT_SERVER = {"env": "shinomilab", "edge_thresholds": [2, 2], "central_threshold": 2, "k_central": 1}
DEFAULT_SETTINGS = {
    "seed": 0,
    "terminals": [],
    "devices": {},
    "max_retries": 2,
    "max_topup_rounds": 1,
    "cooldown_s": 60,
    "timeout_factor": 3,
    "runs_dir": "~/hfl_runs",
    "server": DEFAULT_SERVER,
    "delay_targets": None,
    "delay_probes": None,
}


class TrialsError(ValueError):
    pass


@dataclass(frozen=True)
class Condition:
    name: str
    mode: str
    baseline: str
    num_cycles: int
    delay: Optional[dict]
    n: int


@dataclass(frozen=True)
class Trial:
    seq: int  # 1 始まりの通番
    block: int  # 0 始まり
    condition: Condition
    rep: int  # その条件の何回目か（1 始まり）
    run_tag: str  # 端末の session_complete・実行ディレクトリ名・マニフェストに使う


def order_trials(conditions: List[Condition], seed: int) -> List[Trial]:
    """各ブロックに（n を使い切っていない）各条件を 1 回ずつ入れ、ブロックの中を seed で無作為化する。

    毎試行サーバを起動し直すので、モードの群分けはしない（設計書 §6.2）。グローバル乱数には触らない。
    """
    rng = random.Random(seed)
    trials: List[Trial] = []
    reps = {c.name: 0 for c in conditions}
    for block in range(max((c.n for c in conditions), default=0)):
        members = [c for c in conditions if c.n > block]
        rng.shuffle(members)
        for c in members:
            reps[c.name] += 1
            seq = len(trials) + 1
            trials.append(Trial(seq=seq, block=block, condition=c, rep=reps[c.name],
                                run_tag=f"T{seq:03d}_{c.name}_{reps[c.name]}"))
    return trials


def _settings(raw) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TrialsError("settings は辞書で書く")
    s = copy.deepcopy(DEFAULT_SETTINGS)
    server = {**DEFAULT_SERVER, **(raw.get("server") or {})}
    s.update({k: v for k, v in raw.items() if k != "server"})
    s["server"] = server
    if not s["terminals"]:
        raise TrialsError("settings.terminals が空")
    return s


def _condition(raw, settings: dict, delay_available: bool) -> Condition:
    if not isinstance(raw, dict) or not raw.get("name"):
        raise TrialsError(f"条件には name が要る: {raw!r}")
    name = str(raw["name"])
    mode, baseline = raw.get("mode"), raw.get("baseline", "none")
    if mode not in MODES:
        raise TrialsError(f"{name}: mode は {MODES} のどれか（{mode!r}）")
    if baseline not in BASELINES:
        raise TrialsError(f"{name}: baseline は {BASELINES} のどれか（{baseline!r}）")
    nc, n = raw.get("num_cycles"), raw.get("n")
    if not isinstance(nc, int) or not NUM_CYCLES_MIN <= nc <= NUM_CYCLES_MAX:
        raise TrialsError(f"{name}: num_cycles は {NUM_CYCLES_MIN}〜{NUM_CYCLES_MAX}（{nc!r}）")
    if not isinstance(n, int) or n < 1:
        raise TrialsError(f"{name}: n は 1 以上（{n!r}）")
    delay = raw.get("delay")
    if delay is not None:
        if not isinstance(delay, dict) or set(delay) != set(DELAY_KEYS) or \
                any(not isinstance(delay[k], (int, float)) or delay[k] < 0 for k in DELAY_KEYS):
            raise TrialsError(f"{name}: delay は {{A: ms, B: ms}}（0 以上）で書く（{delay!r}）")
        if not delay_available:
            raise TrialsError(f"{name}: delay の条件があるが、sudo -n で dnctl が動かない（sudoers の設定が要る）")
        for key in ("delay_targets", "delay_probes"):
            v = settings.get(key)
            if not isinstance(v, dict) or any(k not in v for k in DELAY_KEYS):
                raise TrialsError(f"{name}: delay の条件には settings.{key} の A・B が要る（AP_B 側の IP はラボで確かめる）")
        # 遅延の対象は A・B とも要る。probe（ping で確かめる相手）は B が空でもよい（B の WAN は ping に応答しない）が、全体で 1 つは要る
        if any(not settings["delay_targets"][k] for k in DELAY_KEYS) or not any(settings["delay_probes"][k] for k in DELAY_KEYS):
            raise TrialsError(f"{name}: delay の条件には settings.delay_targets の A・B と、settings.delay_probes の 1 つ以上が要る")
        delay = {k: delay[k] for k in DELAY_KEYS}
    return Condition(name=name, mode=mode, baseline=baseline, num_cycles=nc, delay=delay, n=n)


def load_trials(path, delay_available: bool) -> Tuple[dict, List[Condition]]:
    """条件表を読み、検証して (settings, 条件の一覧) を返す。不正なら TrialsError。"""
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise TrialsError(f"YAML として読めない: {e}") from e
    if not isinstance(doc, dict):
        raise TrialsError("条件表は settings と conditions を持つ辞書で書く")
    settings = _settings(doc.get("settings"))
    raws = doc.get("conditions") or []
    if not isinstance(raws, list) or not raws:
        raise TrialsError("conditions が空")
    conditions = [_condition(r, settings, delay_available) for r in raws]
    names = [c.name for c in conditions]
    dup = sorted({x for x in names if names.count(x) > 1})
    if dup:
        raise TrialsError(f"条件名が重複: {dup}")
    return settings, conditions
