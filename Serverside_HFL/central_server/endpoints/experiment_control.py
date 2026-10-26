# Serverside_HFL/central_server/endpoints/experiment_control.py
"""実験制御（ベースラインモード／サイクル数の全体既定＋端末上書き）と実験イベント記録（SP-B, SP3d）。"""
import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from central_server.config import STATE_DIR

router = APIRouter(tags=["experiment_control"])
MODES = ("none", "fixed", "random", "greedy", "central", "hfl_rec", "fixed_ap1")
NUM_CYCLES_MIN, NUM_CYCLES_MAX = 1, 100
CONTROL_PATH: Path = STATE_DIR / "experiment_control.json"
EVENTS_PATH: Path = STATE_DIR / "experiment_events.jsonl"


def _load() -> dict:
    try:
        c = json.loads(CONTROL_PATH.read_text(encoding="utf-8"))
    except Exception:
        c = {}
    c.setdefault("baseline_mode", None)
    c.setdefault("overrides", {})
    c.setdefault("num_cycles", None)
    c.setdefault("num_cycles_overrides", {})
    c.setdefault("version", 0)
    c.setdefault("updated_at", None)
    return c


def _save(c: dict) -> None:
    CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONTROL_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONTROL_PATH)


def _append_event(e: dict) -> None:
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    e = dict(e)
    e.setdefault("received_at", datetime.now().isoformat())
    with EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def resolve_mode(control: dict, terminal_id: Optional[str]) -> Optional[str]:
    """端末上書き > 全体既定。どちらも無ければ None（サーバは指定しない）。"""
    if terminal_id and terminal_id in (control.get("overrides") or {}):
        return control["overrides"][terminal_id]
    return control.get("baseline_mode")


def _valid_num_cycles(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and NUM_CYCLES_MIN <= v <= NUM_CYCLES_MAX


def resolve_num_cycles(control: dict, terminal_id: Optional[str]) -> Optional[int]:
    """SP3d: 端末上書き > 全体既定。どちらも無ければ None（端末のアプリ設定を使う）。"""
    ov = control.get("num_cycles_overrides") or {}
    if terminal_id and terminal_id in ov:
        return ov[terminal_id]
    return control.get("num_cycles")


def apply_control_update(c: dict, body: dict) -> dict:
    """PUT 本文を反映した新しい制御を返す（版数・保存・イベントは呼び出し側）。不正値は ValueError（c は変えない）。

    ベースラインは `baseline_mode` か `clear` があるときだけ、サイクル数は `num_cycles` か `clear_num_cycles` が
    あるときだけ変える。`terminal_id` はどちらにも効く（端末上書き）。どちらも無ければ ValueError。
    """
    c = copy.deepcopy(c)
    tid = body.get("terminal_id")
    touch_baseline = "baseline_mode" in body or bool(body.get("clear"))
    touch_cycles = "num_cycles" in body or bool(body.get("clear_num_cycles"))
    if not touch_baseline and not touch_cycles:
        raise ValueError("baseline_mode / clear / num_cycles / clear_num_cycles のいずれかを指定してください")
    if touch_baseline:
        mode, clear = body.get("baseline_mode"), bool(body.get("clear"))
        if not clear and mode is not None and mode not in MODES:
            raise ValueError(f"unknown baseline_mode: {mode}")
    if touch_cycles:
        n, clear_n = body.get("num_cycles"), bool(body.get("clear_num_cycles"))
        if not clear_n and not _valid_num_cycles(n):
            raise ValueError(f"num_cycles must be an integer in [{NUM_CYCLES_MIN}, {NUM_CYCLES_MAX}]: {n!r}")
    if touch_baseline:
        if tid:
            if clear or mode is None:
                c["overrides"].pop(tid, None)
            else:
                c["overrides"][tid] = mode
        else:
            c["baseline_mode"] = None if clear else mode
    if touch_cycles:
        if tid:
            if clear_n:
                c["num_cycles_overrides"].pop(tid, None)
            else:
                c["num_cycles_overrides"][tid] = n
        else:
            c["num_cycles"] = None if clear_n else n
    return c


def _snapshot(c: dict) -> dict:
    return {"baseline_mode": c["baseline_mode"], "overrides": dict(c["overrides"]),
            "num_cycles": c["num_cycles"], "num_cycles_overrides": dict(c["num_cycles_overrides"])}


@router.get("/api/v1/experiment_control")
def get_control():
    return _load()


@router.put("/api/v1/experiment_control")
async def put_control(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "body must be an object"})
    c = _load()
    try:
        new = apply_control_update(c, body)
    except ValueError as e:
        return JSONResponse(status_code=400, content={
            "detail": str(e), "allowed": list(MODES), "num_cycles_range": [NUM_CYCLES_MIN, NUM_CYCLES_MAX]})
    before = _snapshot(c)
    new["version"] += 1
    new["updated_at"] = datetime.now().isoformat()
    _save(new)
    _append_event({"type": "control_changed", "terminal_id": body.get("terminal_id"), "before": before,
                   "after": _snapshot(new), "version": new["version"]})
    return new


@router.post("/api/v1/experiment_events")
async def post_event(request: Request):
    body = await request.json()
    if not isinstance(body, dict) or not body.get("type"):
        return JSONResponse(status_code=400, content={"detail": "type is required"})
    _append_event(body)
    return {"status": "ok"}


def filter_events(events: list, type_: Optional[str], since: Optional[str], run_tag: Optional[str]) -> list:
    """種別・受信時刻（since 以降）・run_tag で絞る（SP5: ランナーが session_complete を待つ）。since が不正なら ValueError。"""
    since_dt = datetime.fromisoformat(since) if since else None
    out = []
    for e in events:
        if type_ and e.get("type") != type_:
            continue
        if run_tag and e.get("run_tag") != run_tag:
            continue
        if since_dt is not None:
            try:
                if datetime.fromisoformat(str(e.get("received_at"))) < since_dt:
                    continue
            except (ValueError, TypeError):
                continue  # received_at が無い・壊れた行（タイムゾーンの有無の食い違いを含む）は since 指定時に除外
        out.append(e)
    return out


def _read_events() -> list:
    events = []
    try:
        lines = EVENTS_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return events
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue  # 壊れた行は飛ばす
        if isinstance(e, dict):
            events.append(e)
    return events


@router.get("/api/v1/experiment_events")
def list_events(type: Optional[str] = None, since: Optional[str] = None, run_tag: Optional[str] = None):
    try:
        events = filter_events(_read_events(), type, since, run_tag)
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": f"since must be ISO 8601: {since!r}"})
    return {"events": events}
