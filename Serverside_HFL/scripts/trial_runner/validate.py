"""試行の自動検証（純関数）。設計書 §3.4・§6、計画書 Task 4。"""
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from scripts.trial_runner.plan import DEFAULT_SERVER, Trial

START_SKEW_MARK_S = 30


@dataclass
class Verdict:
    valid: bool
    reasons: List[str] = field(default_factory=list)
    marks: List[str] = field(default_factory=list)


def session_rows(ledger_lines: Iterable[dict], run_id: str) -> List[dict]:
    """台帳（追記のみ）から、その run_id のメトリクス行を data_id ごとに最後の行で返す（初出順）。

    同じ data_id はアップロード時と補完時（After など）に複数回追記される。実験イベント（kind=experiment_event）は除く。
    """
    latest: Dict[str, dict] = {}
    for row in ledger_lines:
        if row.get("run_id") != run_id or row.get("kind") == "experiment_event":
            continue
        latest[row.get("data_id")] = row
    return list(latest.values())


def _cycle_rows(rows: List[dict]) -> List[dict]:
    return sorted((r for r in rows if r.get("row_kind") != "baseline_session"),
                  key=lambda r: r.get("cycle_number") or 0)


def validate_trial(trial: Trial, events: List[dict], ledgers: Dict[str, List[dict]], reconcile: dict,
                   server_condition: dict, delay_ok: Optional[bool] = None,
                   terminals: Optional[Sequence[str]] = None,
                   k_central: int = DEFAULT_SERVER["k_central"],
                   edge_thresholds: Sequence[int] = tuple(DEFAULT_SERVER["edge_thresholds"])) -> Verdict:
    """events は当該 run_tag の session_complete と hfl_mode_mismatch、ledgers は端末ごとの session_rows の結果。"""
    c = trial.condition
    tids = sorted(terminals if terminals is not None else ledgers.keys())
    reasons: List[str] = []
    marks: List[str] = []

    if delay_ok is False:
        reasons.append("delay_verify_failed")

    # サーバの条件（Docker 再計測ランナーと同じ確かめ方: エッジ meta と、async は中央ログの K_CENTRAL）
    edges = server_condition.get("edges") or {}
    for i, eid in enumerate(sorted(edges)):
        m = edges[eid] or {}
        if m.get("hfl_mode") != c.mode and "server_condition:hfl_mode" not in reasons:
            reasons.append("server_condition:hfl_mode")
        want = edge_thresholds[i] if i < len(edge_thresholds) else None
        if want is not None and m.get("aggregation_threshold") != want \
                and "server_condition:aggregation_threshold" not in reasons:
            reasons.append("server_condition:aggregation_threshold")
    if not edges:
        reasons.append("server_condition:edges_unknown")
    if c.mode == "async" and set(server_condition.get("k_central_seen") or ()) != {k_central}:
        reasons.append("server_condition:k_central")

    # 完了
    done = {e.get("terminal_id"): e for e in events if e.get("type") == "session_complete"}
    for tid in tids:
        e = done.get(tid)
        if e is None:
            reasons.append(f"incomplete:{tid}")
        elif e.get("result") != "ok":
            reasons.append(f"error:{tid}")

    # 照合
    missing = int(reconcile.get("missing_total") or 0)
    if missing:
        reasons.append(f"reconcile_missing:{missing}")

    # 行数・学習の出所・条件
    for tid in tids:
        rows = ledgers.get(tid) or []
        if c.baseline == "none":
            cyc = _cycle_rows(rows)
            if len(cyc) != c.num_cycles:
                reasons.append(f"rows:{tid}={len(cyc)}")
            provided = 0
            for r in cyc:
                n = r.get("cycle_number")
                src = r.get("init_source")
                # Sync は毎サイクル最新のグローバルモデル（DOWNLOADED）から始まる。Async は新しいグローバルモデルがまだ無いとき、端末が
                # 前サイクルの自モデルで続ける（PROVIDED）のが正常な動き（2026-10-22 利用者了承）。ただしサイクル 1 は DOWNLOADED が必須。
                # RANDOM・RANDOM_LOAD_FAILED などは、どちらのモードでも無効。
                allowed_provided = c.mode == "async" and (n or 0) > 1
                if src == "PROVIDED" and allowed_provided:
                    provided += 1
                elif src != "DOWNLOADED":
                    reasons.append(f"init_source:{tid}:{n}")
                if r.get("round") is None:
                    reasons.append(f"round_null:{tid}:{n}")
            # Async でも、サイクル 2 以降のうち 1 つは DOWNLOADED が要る（全部 PROVIDED = グローバルモデルが一度も届いていない）
            if c.mode == "async" and len(cyc) > 1 and provided == len([r for r in cyc if (r.get("cycle_number") or 0) > 1]) and provided > 0:
                reasons.append(f"init_never_downloaded:{tid}")
            if provided:
                marks.append(f"init_provided:{tid}={provided}")  # 有効のまま。Async で前サイクルの自モデルから始まったサイクルの数
        else:
            summary = [r for r in rows if r.get("row_kind") == "baseline_session"]
            if len(summary) != 1:
                reasons.append(f"rows:{tid}={len(summary)}")
        if any(r.get("baseline_mode") != c.baseline for r in rows):
            reasons.append(f"condition:baseline_mode:{tid}")
        e = done.get(tid)
        if e is not None:
            if e.get("hfl_mode") != c.mode:
                reasons.append(f"condition:hfl_mode:{tid}")
            if e.get("num_cycles") != c.num_cycles:
                reasons.append(f"condition:num_cycles:{tid}")

    # 印（有効のまま）
    for e in events:
        if e.get("type") == "hfl_mode_mismatch":
            marks.append(f"hfl_mode_mismatch:{e.get('terminal_id')}")
    for tid, e in done.items():
        if e.get("after_switch_confirmed") != "true":
            marks.append(f"after_unconfirmed:{tid}")
    starts = [e["started_at_ms"] for e in done.values() if isinstance(e.get("started_at_ms"), (int, float))]
    if len(starts) > 1:
        skew = (max(starts) - min(starts)) / 1000
        if skew > START_SKEW_MARK_S:
            marks.append(f"start_skew:{int(skew)}")

    return Verdict(valid=not reasons, reasons=reasons, marks=sorted(set(marks)))
