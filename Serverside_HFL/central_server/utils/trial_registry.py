# central_server/utils/trial_registry.py
"""
Trial Registry: Experiment run management for HFL.

Each trial is a complete experiment session with:
- start/end timestamps
- configuration parameters (num_rounds, edges, terminals)
- status (running, completed, aborted)
- summary metrics (final accuracy, satisfaction deltas)

Persisted to a JSON file under state/ for durability.
"""
import json
import time
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# Default storage location
_REGISTRY_PATH: Optional[Path] = None


def _get_registry_path() -> Path:
    global _REGISTRY_PATH
    if _REGISTRY_PATH is None:
        try:
            from central_server.config import STORAGE_ROOT
            _REGISTRY_PATH = STORAGE_ROOT / "trial_registry.json"
        except Exception:
            _REGISTRY_PATH = Path(__file__).resolve().parent.parent.parent / "state" / "trial_registry.json"
    _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _REGISTRY_PATH


def _load_registry() -> List[Dict[str, Any]]:
    path = _get_registry_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("trial_registry.json の読み込みに失敗しました。空のレジストリを使用します。")
    return []


def _save_registry(trials: List[Dict[str, Any]]) -> None:
    path = _get_registry_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(trials, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def start_trial(
    *,
    num_rounds: int = 0,
    num_edges: int = 0,
    num_terminals: int = 0,
    description: str = "",
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """Register a new trial and return its trial_id."""
    trial_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    trial = {
        "trial_id": trial_id,
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "ended_at": None,
        "num_rounds": num_rounds,
        "num_edges": num_edges,
        "num_terminals": num_terminals,
        "description": description,
        "config": config or {},
        "metrics_summary": {},
    }
    trials = _load_registry()
    trials.append(trial)
    _save_registry(trials)
    logger.info(f"Trial started: {trial_id}")
    return trial_id


def end_trial(
    trial_id: str,
    *,
    status: str = "completed",
    metrics_summary: Optional[Dict[str, Any]] = None,
) -> None:
    """Mark a trial as ended with optional summary metrics."""
    trials = _load_registry()
    for t in trials:
        if t["trial_id"] == trial_id:
            t["status"] = status
            t["ended_at"] = datetime.now().isoformat()
            if metrics_summary:
                t["metrics_summary"] = metrics_summary
            break
    _save_registry(trials)
    logger.info(f"Trial ended: {trial_id} status={status}")


def update_trial(trial_id: str, **kwargs) -> None:
    """Update arbitrary fields on a trial record."""
    trials = _load_registry()
    for t in trials:
        if t["trial_id"] == trial_id:
            t.update(kwargs)
            break
    _save_registry(trials)


def get_trial(trial_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a single trial by ID."""
    for t in _load_registry():
        if t["trial_id"] == trial_id:
            return t
    return None


def list_trials(status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """List trials, optionally filtered by status, most recent first."""
    trials = _load_registry()
    if status:
        trials = [t for t in trials if t.get("status") == status]
    return list(reversed(trials[-limit:]))


def get_latest_trial() -> Optional[Dict[str, Any]]:
    """Get the most recently started trial."""
    trials = _load_registry()
    return trials[-1] if trials else None
