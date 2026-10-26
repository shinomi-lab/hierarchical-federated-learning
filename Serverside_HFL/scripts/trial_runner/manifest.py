"""試行ごとのマニフェスト（JSON、上書きしない）と朝のサマリ（md）。計画書 Task 5。"""
import json
from collections import Counter, OrderedDict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from scripts.trial_runner.plan import Trial
from scripts.trial_runner.validate import Verdict


def build_manifest(trial: Trial, attempt: int, verdict: Verdict, context: dict, events: List[dict],
                   started_at: datetime, ended_at: datetime, timing: Optional[dict] = None) -> dict:
    timing = timing or {}
    return {
        "run_tag": trial.run_tag,
        "seq": trial.seq,
        "block": trial.block,
        "rep": trial.rep,
        "condition": asdict(trial.condition),
        "attempt": attempt,
        "valid": verdict.valid,
        "reasons": list(verdict.reasons),
        "marks": list(verdict.marks),
        "started_at": started_at.isoformat(timespec="seconds"),
        "ended_at": ended_at.isoformat(timespec="seconds"),
        "duration_s": round((ended_at - started_at).total_seconds(), 1),  # ランナーの所要時間（起動・回収込み、参考）
        # fix-audit-20261026 N1: 全端末の学習区間（端末の時刻から。After を含めない）。timing.train_spans の結果
        "train_span_s": timing.get("train_span_s"),
        "train_span_per_terminal_s": timing.get("per_terminal_s", {}),
        "train_span_missing": timing.get("missing", []),
        "session_complete": [e for e in events if e.get("type") == "session_complete"],
        "other_events": [e for e in events if e.get("type") != "session_complete"],
        **{k: context.get(k) for k in ("git_commit", "git_dirty", "apk_sha256", "seed", "trials_yaml_sha256",
                                      "server_args", "run_dir", "server_condition", "net_delay")},
    }


def write_manifest(out_dir: Path, m: dict) -> Path:
    """`<開始時刻>_<条件>_<seq>.json` に書く。同名があれば `_2`, `_3` … を付ける（上書きしない）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.fromisoformat(m['started_at']):%Y%m%d_%H%M%S}_{m['condition']['name']}_{m['seq']:03d}"
    path, k = out_dir / f"{stem}.json", 1
    while path.exists():
        k += 1
        path = out_dir / f"{stem}_{k}.json"
    with path.open("x", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1, default=str)
    return path


def summarize(manifests: List[dict]) -> str:
    """条件ごとの「有効数 / 試行数（再試行を含む）」・無効の理由の内訳・有効試行の学習区間の平均（主）とランナーの所要時間の平均（参考）。"""
    by_cond: "OrderedDict[str, List[dict]]" = OrderedDict()
    for m in manifests:
        by_cond.setdefault(m["condition"]["name"], []).append(m)
    lines = ["# 無人試行のサマリ", "", "| 条件 | 有効 | 試行 | 学習区間の平均 [s] | ランナーの所要時間の平均 [s]（参考） | 無効の理由 |",
             "|---|---|---|---|---|---|"]
    for name, ms in by_cond.items():
        ok = [m for m in ms if m["valid"]]
        why = Counter(r.split(":", 1)[0] for m in ms if not m["valid"] for r in m["reasons"])
        mean = f"{sum(m['duration_s'] for m in ok) / len(ok):.0f}" if ok else "-"
        spans = [m["train_span_s"] for m in ok if m.get("train_span_s") is not None]
        span = f"{sum(spans) / len(spans):.1f}" if spans else "-"
        why_s = "、".join(f"{k} ×{v}" for k, v in why.most_common()) or "-"
        lines.append(f"| {name} | {len(ok)} | {len(ms)} | {span} | {mean} | {why_s} |")
    marks = Counter(x.split(":", 1)[0] for m in manifests for x in m.get("marks", []))
    if marks:
        lines += ["", "印（有効のまま）: " + "、".join(f"{k} ×{v}" for k, v in marks.most_common())]
    return "\n".join(lines) + "\n"
