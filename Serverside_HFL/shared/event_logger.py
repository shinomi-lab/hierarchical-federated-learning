import json
import time
from pathlib import Path
from typing import Optional, Dict, Any

class EventLogger:
    def __init__(self, component: str, edge_id: Optional[str] = None, instance: Optional[str] = None):
        self.component = component
        self.edge_id = edge_id
        self.instance = instance or edge_id or component
        base = Path("logs") / "events" / component
        base.mkdir(parents=True, exist_ok=True)
        self._path = base / f"{time.strftime('%Y%m%d')}.jsonl"

    def _entry(self, level: str, event: str, fields: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "ts": int(time.time() * 1000),
            "time": time.strftime('%Y-%m-%d %H:%M:%S'),
            "level": level,
            "event": event,
            "component": self.component,
        }
        if self.edge_id:
            entry["edge_id"] = self.edge_id
        if fields:
            entry.update(fields)
        return entry

    def log(self, level: str, event: str, fields: Optional[Dict[str, Any]] = None):
        entry = self._entry(level, event, fields)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def info(self, event: str, fields: Optional[Dict[str, Any]] = None):
        self.log("INFO", event, fields)

    def warn(self, event: str, fields: Optional[Dict[str, Any]] = None):
        self.log("WARN", event, fields)

    def error(self, event: str, fields: Optional[Dict[str, Any]] = None):
        self.log("ERROR", event, fields)

_event_loggers: Dict[str, EventLogger] = {}

def get_event_logger(component: str, edge_id: Optional[str] = None, instance: Optional[str] = None) -> EventLogger:
    key = f"{component}:{edge_id or ''}:{instance or ''}"
    if key not in _event_loggers:
        _event_loggers[key] = EventLogger(component, edge_id=edge_id, instance=instance)
    return _event_loggers[key]
