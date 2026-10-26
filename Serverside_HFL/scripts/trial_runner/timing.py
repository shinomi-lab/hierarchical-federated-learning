"""全端末の学習区間（fix-audit-20261026 N1）。ランナーのマニフェストと再集計スクリプト（trial_timing_report.py）が同じ定義を使う。

学習区間 = 最も早いセッションの開始（session_complete の started_at_ms）〜 最も遅い「最終サイクルの終了」。
最終サイクルの終了は、台帳のそのセッション（run_id）の行のうち cycle_number が最大で、After の補完前（satisfaction_after が null）の版の
timestamp_ms の最大。After の補完は最終サイクルの行の timestamp_ms を After の時刻で上書きするので、その版は使わない。
"""
from typing import Dict, List, Optional


def _train_end_ms(rows: List[dict], run_id: Optional[str]) -> Optional[int]:
    cyc = [r for r in rows if r.get("run_id") == run_id and isinstance(r.get("cycle_number"), int)]
    if not cyc:
        return None
    last = max(r["cycle_number"] for r in cyc)
    ts = [r.get("timestamp_ms") for r in cyc
          if r["cycle_number"] == last and r.get("satisfaction_after") is None and isinstance(r.get("timestamp_ms"), (int, float))]
    return max(ts) if ts else None


def train_spans(events: List[dict], ledgers: Dict[str, List[dict]]) -> dict:
    """events: session_complete の一覧。ledgers: 端末 ID → 台帳の行（全ての版）。
    戻り値: train_span_s（求められた端末だけで計算。無ければ None）・per_terminal_s（端末 → 秒 / None）・missing（終了が求められない端末）。"""
    per, starts, ends, missing = {}, [], [], []
    for e in events:
        if e.get("type", "session_complete") != "session_complete":
            continue
        tid, start = e.get("terminal_id"), e.get("started_at_ms")
        end = _train_end_ms(ledgers.get(tid) or [], e.get("run_id"))
        if end is None or not isinstance(start, (int, float)):
            per[tid] = None
            missing.append(tid)
            continue
        per[tid] = round((end - start) / 1000, 1)
        starts.append(start)
        ends.append(end)
    span = round((max(ends) - min(starts)) / 1000, 1) if ends else None
    return {"train_span_s": span, "per_terminal_s": per, "missing": sorted(missing)}
