#!/usr/bin/env python3
"""
最新（または指定）の実機試行ログを集約し、分析レポートを **ファイルに保存** する。

取り込むデータ源:
  - logs/time_records/central_server_*.log
  - logs/time_records/edge_server_edge-server-*.log / logs/rounds/r*/edge_server_*.log
  - logs/events/edge_terminal_update/*.jsonl  (upload timing)
  - logs/events/edge_client_logs/*.jsonl
  - received_files/terminal_telemetry/*/index.jsonl  (rich device/network/training data)
  - received_files/terminal_updates/**/manifest_*.json
  - received_edges/r*/edge-server-*/agg_*.meta.json  (edge aggregation metadata)

使用例:
  cd Serverside_HFL
  python3 scripts/analyze_latest_trial.py
  python3 scripts/analyze_latest_trial.py --central logs/time_records/central_server_20260420_185545.log
  python3 scripts/analyze_latest_trial.py --json-summary
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
BRACKET_TS = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\]")


def _parse_ts(s) -> Optional[datetime]:
    if s is None:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:26], fmt) if fmt.endswith("%f") else datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _safe_float(*candidates):
    for v in candidates:
        if v is None or v == "null":
            continue
        try:
            f = float(v)
            if f != f:
                continue
            return f
        except (ValueError, TypeError):
            continue
    return None


def _safe_int(*candidates):
    for v in candidates:
        if v is None or v == "null":
            continue
        try:
            return int(float(v))
        except (ValueError, TypeError):
            continue
    return None


def find_latest_central(logs_dir: Path) -> Optional[Path]:
    files = sorted(logs_dir.glob("central_server_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def central_time_window(central_path: Path) -> Tuple[Optional[datetime], Optional[datetime]]:
    t0, t1 = None, None
    try:
        text = central_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, None
    for m in BRACKET_TS.finditer(text):
        t = _parse_ts(m.group(1))
        if t is None:
            continue
        if t0 is None or t < t0:
            t0 = t
        if t1 is None or t > t1:
            t1 = t
    return t0, t1


def iter_jsonl_lines(path: Path) -> Iterable[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def json_blobs_from_central_line(line: str) -> List[dict]:
    out = []
    for m in re.finditer(r":\s*(\{.*\})\s*$", line):
        try:
            out.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass
    for m in re.finditer(r"\] (\w+): (\{.*\})\s*$", line):
        try:
            out.append(json.loads(m.group(2)))
        except json.JSONDecodeError:
            pass
    return out


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class TrialReport:
    central_path: Path
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    edge_updates: List[Dict[str, Any]] = field(default_factory=list)
    aggregations: List[Dict[str, Any]] = field(default_factory=list)
    models_received: List[Dict[str, Any]] = field(default_factory=list)
    telemetry_rows: List[Dict[str, Any]] = field(default_factory=list)
    weights_received_rows: List[Dict[str, Any]] = field(default_factory=list)
    manifests: List[Tuple[Path, Dict[str, Any]]] = field(default_factory=list)
    post_switch_telemetry: List[Dict[str, Any]] = field(default_factory=list)
    # NEW: rich telemetry from index.jsonl
    index_telemetry: List[Dict[str, Any]] = field(default_factory=list)
    # NEW: upload timing events
    upload_events: List[Dict[str, Any]] = field(default_factory=list)
    # NEW: edge aggregation metadata
    edge_agg_meta: List[Dict[str, Any]] = field(default_factory=list)
    # NEW: central server timing events
    central_timing: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Data ingestion
# ---------------------------------------------------------------------------
def parse_central(central_path: Path, rep: TrialReport) -> None:
    rep.window_start, rep.window_end = central_time_window(central_path)
    try:
        lines = central_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as e:
        print(f"WARN: cannot read central: {e}", file=sys.stderr)
        return
    for line in lines:
        # Extract bracket timestamp for this line
        line_ts = None
        ts_match = BRACKET_TS.search(line)
        if ts_match:
            line_ts = ts_match.group(1)

        if "edge_update_entry:" in line:
            for jo in json_blobs_from_central_line(line):
                if "edge_id" in jo and "round" in jo:
                    rep.edge_updates.append(jo)
        if "aggregation_completed:" in line:
            for jo in json_blobs_from_central_line(line):
                if "round" in jo or "next_round" in jo:
                    if line_ts:
                        jo["_central_ts"] = line_ts
                    rep.aggregations.append(jo)
                    rep.central_timing.append({"event": "aggregation_completed", "ts": line_ts, **jo})
        if "training_metrics_received:" in line:
            for jo in json_blobs_from_central_line(line):
                rep.telemetry_rows.append({"file": "central_server.log", "ts": jo.get("event_timestamp"), **jo})
        if "model_received:" in line and "edge_update" not in line:
            for jo in json_blobs_from_central_line(line):
                if "edge_id" in jo:
                    if line_ts:
                        jo["_central_ts"] = line_ts
                    rep.models_received.append(jo)
                    rep.central_timing.append({"event": "model_received", "ts": line_ts, **jo})
        if "request_e2e:" in line or "request_duration" in line:
            for jo in json_blobs_from_central_line(line):
                rep.central_timing.append({"event": "request_e2e", "ts": line_ts, **jo})
        if "aggregation_metrics:" in line:
            for jo in json_blobs_from_central_line(line):
                rep.central_timing.append({"event": "aggregation_metrics", "ts": line_ts, **jo})


def ingest_jsonl_file(path: Path, rep: TrialReport, window: Tuple[Optional[datetime], Optional[datetime]]) -> int:
    w0, w1 = window
    if w0 and w1:
        w0 = w0 - timedelta(minutes=2)
        w1 = w1 + timedelta(minutes=2)
    n = 0
    for row in iter_jsonl_lines(path):
        ts = _parse_ts(str(row.get("ts", "")).replace("T", " "))
        if w0 and w1 and ts and (ts < w0 or ts > w1):
            continue
        ev = row.get("event")
        det = row.get("details") or {}
        if ev == "telemetry_received":
            rep.telemetry_rows.append({"file": str(path.name), "ts": row.get("ts"), **det})
            n += 1
        elif ev == "weights_received":
            rep.weights_received_rows.append({"file": str(path.name), "ts": row.get("ts"), **det})
            n += 1
    return n


def collect_round_and_edge_logs(window: Tuple[Optional[datetime], Optional[datetime]]) -> List[Path]:
    w0, w1 = window
    paths: List[Path] = []
    tr = ROOT / "logs" / "time_records"
    rd = ROOT / "logs" / "rounds"
    for base in (tr, rd):
        if not base.exists():
            continue
        for p in base.rglob("edge_server*.log"):
            if "organized" in str(p):
                continue
            if w0 and w1:
                ts_line = None
                try:
                    with p.open(encoding="utf-8", errors="ignore") as fh:
                        for line in fh:
                            line = line.strip()
                            if line.startswith("{"):
                                try:
                                    j = json.loads(line)
                                    ts_line = _parse_ts(str(j.get("ts", "")))
                                except json.JSONDecodeError:
                                    pass
                                break
                except OSError:
                    continue
                if ts_line and (ts_line < w0 - timedelta(hours=1) or ts_line > w1 + timedelta(hours=1)):
                    continue
            paths.append(p)
    return sorted(set(paths), key=lambda x: str(x))


def load_post_switch_telemetry(window: Tuple[Optional[datetime], Optional[datetime]]) -> List[Dict[str, Any]]:
    w0, w1 = window
    w0_ms = int(w0.timestamp() * 1000) - 5 * 60 * 1000 if w0 else None
    w1_ms = int(w1.timestamp() * 1000) + 5 * 60 * 1000 if w1 else None
    base = ROOT / "received_files" / "terminal_telemetry"
    out: List[Dict[str, Any]] = []
    if not base.exists():
        return out
    for idx_path in base.rglob("index.jsonl"):
        try:
            with idx_path.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_ms = ev.get("server_received_ts_ms") or ev.get("timestamp_ms")
                    if ts_ms is not None and w0_ms and w1_ms:
                        if int(ts_ms) < w0_ms or int(ts_ms) > w1_ms:
                            continue
                    if ev.get("terminal_satisfaction_after") is not None:
                        out.append(ev)
        except OSError:
            continue
    return out


def load_full_index_telemetry(window: Tuple[Optional[datetime], Optional[datetime]]) -> List[Dict[str, Any]]:
    """Load ALL entries from index.jsonl — the richest data source."""
    w0, w1 = window
    w0_ms = int(w0.timestamp() * 1000) - 5 * 60 * 1000 if w0 else None
    w1_ms = int(w1.timestamp() * 1000) + 5 * 60 * 1000 if w1 else None
    base = ROOT / "received_files" / "terminal_telemetry"
    out: List[Dict[str, Any]] = []
    if not base.exists():
        return out
    for idx_path in base.rglob("index.jsonl"):
        for row in iter_jsonl_lines(idx_path):
            ts_ms = row.get("server_received_ts_ms") or row.get("timestamp_ms")
            if ts_ms is not None and w0_ms and w1_ms:
                if int(ts_ms) < w0_ms or int(ts_ms) > w1_ms:
                    continue
            out.append(row)
    return out


def load_upload_events(window: Tuple[Optional[datetime], Optional[datetime]]) -> List[Dict[str, Any]]:
    """Load upload timing events from logs/events/edge_terminal_update/."""
    w0, w1 = window
    base = ROOT / "logs" / "events" / "edge_terminal_update"
    out: List[Dict[str, Any]] = []
    if not base.exists():
        return out
    for p in sorted(base.glob("*.jsonl")):
        for row in iter_jsonl_lines(p):
            if w0 and w1:
                ts_str = row.get("time", "")
                ts = _parse_ts(ts_str)
                if ts and (ts < w0 - timedelta(minutes=5) or ts > w1 + timedelta(minutes=5)):
                    continue
            out.append(row)
    return out


def load_edge_agg_meta(window: Tuple[Optional[datetime], Optional[datetime]]) -> List[Dict[str, Any]]:
    """Load edge aggregation metadata from received_edges/r*/edge-server-*/agg_*.meta.json."""
    base = ROOT / "received_edges"
    out: List[Dict[str, Any]] = []
    if not base.exists():
        return out
    for p in sorted(base.rglob("agg_*.meta.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            # Extract round from parent dir name
            round_dir = p.parent.parent.name  # e.g. "r1"
            if round_dir.startswith("r"):
                data["_round_from_path"] = int(round_dir[1:])
            data["_edge_from_path"] = p.parent.name  # e.g. "edge-server-01"
            out.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return out


def _enrich_telemetry_from_index(rep: TrialReport) -> None:
    """Merge training_metrics_last from index_telemetry into telemetry_rows."""
    existing_keys = set()
    for row in rep.telemetry_rows:
        tid = row.get("terminal_id") or row.get("edge_id")
        rid = row.get("round_id") or row.get("round")
        if tid and rid is not None:
            existing_keys.add((str(tid), str(rid)))

    for ev in rep.index_telemetry:
        tml = ev.get("training_metrics_last")
        if not isinstance(tml, dict):
            continue
        tid = tml.get("terminal_id") or ev.get("device_id")
        rid = tml.get("round") or ev.get("global_round")
        if not tid or rid is None:
            continue

        acc = tml.get("accuracy") or tml.get("val_accuracy")
        loss = tml.get("loss") or tml.get("val_loss")
        sat_before = tml.get("satisfaction_before")
        sat_after = ev.get("terminal_satisfaction_after") or tml.get("satisfaction_after")
        app_type = tml.get("app_type")
        cycle = tml.get("cycle_number")

        key = (str(tid), str(rid))
        if key in existing_keys:
            for row in rep.telemetry_rows:
                row_tid = row.get("terminal_id") or row.get("edge_id")
                row_rid = row.get("round_id") or row.get("round")
                if str(row_tid) == key[0] and str(row_rid) == key[1]:
                    if row.get("accuracy") is None and acc is not None:
                        row["accuracy"] = acc
                    if row.get("loss") is None and loss is not None:
                        row["loss"] = loss
                    if row.get("satisfaction_after") is None and sat_after is not None:
                        row["satisfaction_after"] = sat_after
                    if row.get("app_type") is None and app_type is not None:
                        row["app_type"] = app_type
                    break
        else:
            rep.telemetry_rows.append({
                "file": "index.jsonl",
                "terminal_id": tid,
                "round_id": rid,
                "round": rid,
                "accuracy": acc,
                "loss": loss,
                "app_type": app_type,
                "satisfaction_before": sat_before,
                "satisfaction_after": sat_after,
                "client_meta": json.dumps(tml),
                "cycle_number": cycle,
            })
            existing_keys.add(key)


def load_manifests_for_window(
    window: Tuple[Optional[datetime], Optional[datetime]],
    manifest_subglob: str,
) -> List[Tuple[Path, Dict[str, Any]]]:
    w0, w1 = window
    out: List[Tuple[Path, Dict[str, Any]]] = []
    base = ROOT / "received_files" / "terminal_updates"
    if not base.exists():
        return out
    if "**" in manifest_subglob:
        pattern = manifest_subglob.split("**/")[-1]
        candidates = base.rglob(pattern)
    else:
        candidates = ROOT.glob(manifest_subglob)
    for p in candidates:
        if not p.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime)
        except OSError:
            continue
        if w0 and w1 and (mtime < w0 - timedelta(minutes=30) or mtime > w1 + timedelta(hours=2)):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append((p, data))
    return sorted(out, key=lambda x: str(x[0]))


# ---------------------------------------------------------------------------
# DataFrame builders
# ---------------------------------------------------------------------------
def _client_meta_dict(client_meta: Any) -> Dict[str, Any]:
    if isinstance(client_meta, str) and client_meta.strip().startswith("{"):
        try:
            return json.loads(client_meta)
        except json.JSONDecodeError:
            return {}
    if isinstance(client_meta, dict):
        return client_meta
    return {}


def _telemetry_dataframe(rep: TrialReport):
    try:
        import pandas as pd
    except ImportError:
        return None
    rows: List[Dict[str, Any]] = []
    for row in rep.telemetry_rows:
        tid = row.get("terminal_id") or row.get("edge_id")
        if not tid:
            continue
        cm = _client_meta_dict(row.get("client_meta"))
        ep = cm.get("epoch_durations_ms") or []
        if isinstance(ep, str):
            try:
                ep = json.loads(ep)
            except (json.JSONDecodeError, ValueError):
                ep = []
        if isinstance(ep, list) and ep:
            mean_ep = float(sum(float(x) for x in ep)) / len(ep)
        else:
            mean_ep = None

        sat_before = row.get("satisfaction_before")
        if sat_before is None:
            sat_before = cm.get("satisfaction_before")
        sat_after = row.get("satisfaction_after")
        if sat_after is None:
            sat_after = cm.get("satisfaction_after")
        if sat_after is None:
            sat_after = row.get("terminal_satisfaction_after")

        rows.append({
            "terminal_id":         tid,
            "round_id":            row.get("round_id") or row.get("round"),
            "cycle_number":        cm.get("cycle_number") or row.get("cycle_number"),
            "total_local_ms":      _safe_float(cm.get("total_local_ms")),
            "mean_epoch_ms":       mean_ep,
            "app_type":            cm.get("app_type") or row.get("app_type"),
            "app_index":           _safe_int(cm.get("app_index")),
            "satisfaction_before": _safe_float(sat_before),
            "satisfaction_after":  _safe_float(sat_after),
            "accuracy":            _safe_float(row.get("accuracy"), cm.get("accuracy")),
            "loss":                _safe_float(row.get("loss"), cm.get("loss")),
            "val_accuracy":        _safe_float(cm.get("val_accuracy")),
            "val_loss":            _safe_float(cm.get("val_loss")),
            "epoch_count":         _safe_int(cm.get("epoch_count")),
            "data_samples_count":  _safe_int(cm.get("data_samples_count")),
            "battery_at_start":    _safe_float(cm.get("battery_at_start")),
            "battery_at_end":      _safe_float(cm.get("battery_at_end")),
            "mem_used_start":      _safe_float(cm.get("mem_used_mb_at_start")),
            "mem_used_end":        _safe_float(cm.get("mem_used_mb_at_end")),
            "predicted_ap":        row.get("predicted_ap"),
            "weight_norm":         _safe_float(row.get("weight_norm")),
            "request_duration_ms": _safe_float(row.get("request_duration_ms")),
        })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["terminal_id", "round_id", "cycle_number"], keep="last")
    df = df.sort_values(["terminal_id", "round_id"])

    # Fill satisfaction_after from post_switch_telemetry
    if rep.post_switch_telemetry:
        after_map: Dict[Tuple[str, Any], float] = {}
        for ev in rep.post_switch_telemetry:
            tid = ev.get("terminal_id") or ev.get("device_id")
            sa = ev.get("terminal_satisfaction_after")
            rnd = ev.get("current_round_local") or ev.get("last_applied_round")
            if tid and sa is not None and rnd is not None:
                after_map[(str(tid), int(rnd))] = float(sa)
        if after_map:
            def _fill_after(r):
                if r["satisfaction_after"] is not None:
                    return r["satisfaction_after"]
                return after_map.get((str(r["terminal_id"]), int(r["round_id"])) if r["round_id"] is not None else ("", -1))
            df["satisfaction_after"] = df.apply(_fill_after, axis=1)
    return df


def _index_telemetry_dataframe(rep: TrialReport):
    """Build a rich DataFrame from index.jsonl with device/network/resource fields."""
    try:
        import pandas as pd
    except ImportError:
        return None
    if not rep.index_telemetry:
        return None
    rows = []
    for ev in rep.index_telemetry:
        tml = ev.get("training_metrics_last") or {}
        sat_meta = ev.get("terminal_satisfaction_meta") or {}
        tid = ev.get("terminal_id") or ev.get("device_id") or tml.get("terminal_id")
        rid = tml.get("round") or ev.get("global_round") or ev.get("current_round_local")

        # Extract app_scores RTT & bandwidth
        app_scores = sat_meta.get("app_scores") or []
        measured_rtt = None
        measured_bw = None
        if app_scores:
            for sc in app_scores:
                if sc.get("measured_rtt") is not None:
                    measured_rtt = _safe_float(sc["measured_rtt"])
                if sc.get("indicator") == "Throughput" and sc.get("measured_throughput") is not None:
                    measured_bw = _safe_float(sc["measured_throughput"])

        rows.append({
            "terminal_id":       tid,
            "round_id":          rid,
            "device_model":      ev.get("device_model"),
            "device_manufacturer": ev.get("device_manufacturer"),
            "os_sdk_int":        _safe_int(ev.get("os_sdk_int")),
            "network_type":      ev.get("network_type"),
            "battery_level":     _safe_float(ev.get("battery_level")),
            "wifi_rssi_dbm":     _safe_int(ev.get("wifi_rssi_dbm")),
            "wifi_link_speed":   _safe_int(ev.get("wifi_link_speed")),
            "storage_free_mb":   _safe_float(ev.get("storage_free_mb")),
            "mem_used_mb":       _safe_float(ev.get("mem_used_mb")),
            "mem_percent":       _safe_float(ev.get("mem_percent")),
            "process_rss_mb":    _safe_float(ev.get("process_rss_mb")),
            "process_vms_mb":    _safe_float(ev.get("process_vms_mb")),
            "accuracy":          _safe_float(tml.get("accuracy")),
            "loss":              _safe_float(tml.get("loss")),
            "total_local_ms":    _safe_float(tml.get("total_local_ms")),
            "epoch_count":       _safe_int(tml.get("epoch_count")),
            "data_samples_count": _safe_int(tml.get("data_samples_count")),
            "cycle_number":      _safe_int(tml.get("cycle_number")),
            "app_type":          tml.get("app_type"),
            "app_index":         _safe_int(tml.get("app_index")),
            "satisfaction_before": _safe_float(tml.get("satisfaction_before")),
            "satisfaction_after":  _safe_float(ev.get("terminal_satisfaction_after")),
            "satisfaction_avg":  _safe_float(sat_meta.get("satisfaction_avg")),
            "sat_rtt_ms":        _safe_float(sat_meta.get("server_rtt_ms")),
            "sat_gateway_rtt_ms": _safe_float(sat_meta.get("gateway_rtt_ms")),
            "sat_bandwidth_mbps": _safe_float(sat_meta.get("bandwidth_mbps")),
            "measured_rtt":      measured_rtt,
            "measured_bw":       measured_bw,
            "battery_at_start":  _safe_float(tml.get("battery_at_start")),
            "battery_at_end":    _safe_float(tml.get("battery_at_end")),
            "mem_used_start":    _safe_float(tml.get("mem_used_mb_at_start")),
            "mem_used_end":      _safe_float(tml.get("mem_used_mb_at_end")),
            "timestamp_ms":      _safe_int(ev.get("server_received_ts_ms") or ev.get("timestamp_ms")),
            "edge_server_id":    ev.get("edge_server_id"),
        })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["terminal_id", "round_id", "cycle_number"], keep="last")
    df = df.sort_values(["terminal_id", "round_id"])
    return df


def _upload_timing_dataframe(rep: TrialReport):
    """Build DataFrame of upload durations per terminal per round from event logs."""
    try:
        import pandas as pd
    except ImportError:
        return None
    if not rep.upload_events:
        return None
    # Group by (edge_id, terminal_id, round_id) and compute duration from receive_start to manifest_saved
    starts = {}
    ends = {}
    for ev in rep.upload_events:
        event = ev.get("event")
        key = (ev.get("edge_id", ""), ev.get("terminal_id", ""), ev.get("round_id", ""))
        ts_ms = ev.get("ts")
        if ts_ms is None:
            continue
        if event == "weights_receive_start":
            if key not in starts or ts_ms < starts[key]:
                starts[key] = ts_ms
        elif event == "weights_manifest_saved":
            if key not in ends or ts_ms > ends[key]:
                ends[key] = ts_ms

    rows = []
    for key, t0 in starts.items():
        t1 = ends.get(key)
        if t1 is not None:
            rows.append({
                "edge_id": key[0],
                "terminal_id": key[1],
                "round_id": key[2],
                "upload_duration_ms": t1 - t0,
            })
    if not rows:
        return None
    return pd.DataFrame(rows).sort_values(["terminal_id", "round_id"])


def _edge_agg_dataframe(rep: TrialReport):
    """Build DataFrame from edge aggregation metadata."""
    try:
        import pandas as pd
    except ImportError:
        return None
    if not rep.edge_agg_meta:
        return None
    rows = []
    for meta in rep.edge_agg_meta:
        rows.append({
            "round": meta.get("round") or meta.get("_round_from_path"),
            "edge_id": meta.get("edge_id") or meta.get("_edge_from_path"),
            "num_clients": _safe_int(meta.get("num_clients")),
            "sum_n_samples": _safe_int(meta.get("sum_n_samples")),
            "run_id": meta.get("run_id") or meta.get("federation_run_id"),
        })
    if not rows:
        return None
    return pd.DataFrame(rows).sort_values(["round", "edge_id"])


# ---------------------------------------------------------------------------
# Plot helpers
# ---------------------------------------------------------------------------
def _setup_matplotlib():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    # Enable CJK font support (try common macOS/Linux CJK fonts)
    from matplotlib import rcParams
    for font in ["Hiragino Sans", "Hiragino Maru Gothic Pro", "IPAexGothic", "Noto Sans CJK JP", "Arial Unicode MS"]:
        try:
            from matplotlib.font_manager import FontProperties
            fp = FontProperties(family=font)
            if fp.get_name() != font:
                continue
            rcParams["font.family"] = font
            break
        except Exception:
            continue
    return plt


def _get_colors(n: int):
    plt = _setup_matplotlib()
    cmap = plt.get_cmap("tab10")
    return [cmap(i % 10) for i in range(n)]


def _harmonic_mean(vals):
    vals = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v)) and v > 0]
    if not vals:
        return float("nan")
    return len(vals) / sum(1.0 / v for v in vals)


# ---------------------------------------------------------------------------
# Plot functions
# ---------------------------------------------------------------------------
def write_accuracy_loss_plots(df, run_dir: Path) -> List[str]:
    plt = _setup_matplotlib()
    names = []

    if "accuracy" not in df.columns or not df["accuracy"].notna().any():
        return names

    import numpy as np

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    cmap = plt.get_cmap("tab10")
    for i, (tid, g) in enumerate(df.groupby("terminal_id")):
        g2 = g.sort_values("round_id")
        c = cmap(i % 10)
        ax1.plot(g2["round_id"], g2["accuracy"], marker="o", label=f"{tid} Acc", color=c, alpha=0.7)
        if "loss" in g2.columns:
            ax2.plot(g2["round_id"], g2["loss"], marker="x", linestyle="--", label=f"{tid} Loss", color=c, alpha=0.7)

    # System mean with std band
    grouped_acc = df.groupby("round_id")["accuracy"]
    means_acc = grouped_acc.mean().sort_index()
    stds_acc = grouped_acc.std().sort_index().fillna(0)
    rx = means_acc.index.astype(float)
    ax1.plot(rx, means_acc.values, color="black", linewidth=2.5, linestyle="--", marker="D",
             markersize=5, label="System Mean", zorder=5)
    ax1.fill_between(rx, (means_acc - stds_acc).values, (means_acc + stds_acc).values,
                     color="black", alpha=0.1, label="1 std")

    if "loss" in df.columns and df["loss"].notna().any():
        grouped_loss = df.groupby("round_id")["loss"]
        means_loss = grouped_loss.mean().sort_index()
        stds_loss = grouped_loss.std().sort_index().fillna(0)
        ax2.plot(rx, means_loss.values, color="black", linewidth=2.5, linestyle="--", marker="D",
                 markersize=5, label="System Mean", zorder=5)
        ax2.fill_between(rx, (means_loss - stds_loss).values, (means_loss + stds_loss).values,
                         color="black", alpha=0.1, label="1 std")

    ax1.set_ylabel("Accuracy")
    ax1.set_title("Local Model Accuracy & Loss Trends (shading = 1 std across terminals)")
    ax1.legend(loc="lower right", fontsize=7)
    ax1.grid(True, alpha=0.3)
    ax2.set_ylabel("Loss")
    ax2.set_xlabel("Round ID")
    ax2.legend(loc="upper right", fontsize=7)
    ax2.grid(True, alpha=0.3)
    fig.tight_layout()
    p = run_dir / "plot_accuracy_loss.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    names.append(p.name)

    # Accuracy delta (improvement rate between rounds)
    if df["accuracy"].notna().sum() > 2:
        fig, ax = plt.subplots(figsize=(10, 5))
        for tid, g in df.groupby("terminal_id"):
            g2 = g.sort_values("round_id").dropna(subset=["accuracy"])
            if len(g2) < 2:
                continue
            deltas = g2["accuracy"].diff()
            ax.bar(g2["round_id"].iloc[1:].astype(float) + (list(df["terminal_id"].unique()).index(tid) - 1) * 0.15,
                   deltas.iloc[1:], width=0.15, alpha=0.7, label=tid)
        ax.axhline(y=0, color="black", linewidth=0.5)
        ax.set_xlabel("Round ID")
        ax.set_ylabel("Accuracy Change (delta)")
        ax.set_title("Per-Round Accuracy Improvement (delta)")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        p = run_dir / "plot_accuracy_delta.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    return names


def write_training_time_plots(df, run_dir: Path) -> List[str]:
    plt = _setup_matplotlib()
    names = []

    # Local training duration per round
    if "total_local_ms" in df.columns and df["total_local_ms"].notna().any():
        fig, ax = plt.subplots(figsize=(10, 5))
        for tid, g in df.groupby("terminal_id"):
            g2 = g.sort_values("round_id").dropna(subset=["total_local_ms"])
            if g2.empty:
                continue
            ax.plot(g2["round_id"].astype(float), g2["total_local_ms"].astype(float), marker="o", label=tid)
        ax.set_xlabel("Round ID")
        ax.set_ylabel("Total Local Training (ms)")
        ax.set_title("Local Training Duration per Round")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        p = run_dir / "plot_local_ms_by_round.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    # Mean epoch duration
    if "mean_epoch_ms" in df.columns and df["mean_epoch_ms"].notna().any():
        fig, ax = plt.subplots(figsize=(10, 5))
        for tid, g in df.groupby("terminal_id"):
            g2 = g.sort_values("round_id").dropna(subset=["mean_epoch_ms"])
            if g2.empty:
                continue
            ax.plot(g2["round_id"].astype(float), g2["mean_epoch_ms"].astype(float), marker="s", label=tid)
        ax.set_xlabel("Round ID")
        ax.set_ylabel("Mean Epoch Duration (ms)")
        ax.set_title("Mean Epoch Duration per Round")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        p = run_dir / "plot_mean_epoch_ms.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    return names


def write_satisfaction_plots(df, rep: TrialReport, run_dir: Path) -> List[str]:
    """Before→After の2点折れ線で各端末の満足度変化を示す。"""
    plt = _setup_matplotlib()
    names = []

    has_sat = df["satisfaction_before"].notna().any() or df["satisfaction_after"].notna().any()
    if not has_sat:
        return names

    # 端末ごとに Before/After の平均値を算出
    terminals = sorted(df["terminal_id"].unique())
    bef_vals = []
    aft_vals = []
    valid_terminals = []
    for tid in terminals:
        g = df[df["terminal_id"] == tid]
        bef = g["satisfaction_before"].dropna()
        aft = g["satisfaction_after"].dropna()
        if bef.empty and aft.empty:
            continue
        bef_vals.append(bef.mean() if not bef.empty else None)
        aft_vals.append(aft.mean() if not aft.empty else None)
        valid_terminals.append(tid)

    if not valid_terminals:
        return names

    fig, ax = plt.subplots(figsize=(8, 6))
    x = [0, 1]  # 0=Before, 1=After

    # 重なり回避: Before/After それぞれで同じ値の端末をずらす
    def _dodge_values(vals, spread=0.012):
        """同じ値が複数あるとき、上下に均等にずらしたリストを返す。"""
        dodged = list(vals)  # copy
        from collections import Counter
        # Before側 (index in vals)
        groups = defaultdict(list)
        for i, v in enumerate(vals):
            if v is not None:
                # 丸めて近い値もグルーピング (0.01刻み)
                key = round(v, 2)
                groups[key].append(i)
        for key, indices in groups.items():
            if len(indices) <= 1:
                continue
            n = len(indices)
            for rank, idx in enumerate(indices):
                offset = (rank - (n - 1) / 2.0) * spread
                dodged[idx] = vals[idx] + offset
        return dodged

    bef_dodged = _dodge_values(bef_vals)
    aft_dodged = _dodge_values(aft_vals)

    # 各端末を同じ色の折れ線で描画
    for i, tid in enumerate(valid_terminals):
        b = bef_dodged[i]
        a = aft_dodged[i]
        b_raw = bef_vals[i]
        a_raw = aft_vals[i]
        if b_raw is not None and a_raw is not None:
            ax.plot(x, [b, a], marker="o", color="steelblue", linewidth=2, markersize=8, alpha=0.6)
            ax.annotate(tid, xy=(1, a), xytext=(1.05, a), fontsize=8, va="center")
        elif b_raw is not None:
            ax.plot(0, b, marker="o", color="steelblue", markersize=8, alpha=0.6)
            ax.annotate(tid, xy=(0, b), xytext=(0.05, b), fontsize=8, va="center")
        elif a_raw is not None:
            ax.plot(1, a, marker="o", color="steelblue", markersize=8, alpha=0.6)
            ax.annotate(tid, xy=(1, a), xytext=(1.05, a), fontsize=8, va="center")

    # システム全体の平均線 (太く強調)
    all_bef = [v for v in bef_vals if v is not None]
    all_aft = [v for v in aft_vals if v is not None]
    sys_bef = _harmonic_mean(all_bef) if all_bef else None
    sys_aft = _harmonic_mean(all_aft) if all_aft else None
    if sys_bef is not None and not math.isnan(sys_bef) and sys_aft is not None and not math.isnan(sys_aft):
        ax.plot(x, [sys_bef, sys_aft], marker="D", color="darkred", linewidth=3.5, markersize=12, zorder=5, label="System HM")
    elif sys_bef is not None and not math.isnan(sys_bef):
        ax.plot(0, sys_bef, marker="D", color="darkred", markersize=12, zorder=5, label="System HM (Before only)")

    ax.set_xticks(x)
    ax.set_xticklabels(["Before", "After"], fontsize=12)
    ax.set_xlim(-0.3, 1.6)
    ax.set_ylabel("Satisfaction (0-1)")
    ax.set_title("Terminal Satisfaction: Before vs After")
    ax.grid(True, alpha=0.3, axis="y")
    if any(l.get_label().startswith("System") for l in ax.get_lines()):
        ax.legend(fontsize=10)
    fig.tight_layout()
    p = run_dir / "plot_satisfaction_combined.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    names.append(p.name)

    return names


def write_network_qos_plots(idx_df, run_dir: Path) -> List[str]:
    """WiFi RSSI, RTT, Bandwidth from index.jsonl rich data."""
    plt = _setup_matplotlib()
    names = []
    if idx_df is None or idx_df.empty:
        return names

    # WiFi RSSI over rounds
    if "wifi_rssi_dbm" in idx_df.columns and idx_df["wifi_rssi_dbm"].notna().any():
        fig, ax = plt.subplots(figsize=(10, 5))
        for tid, g in idx_df.groupby("terminal_id"):
            g2 = g.sort_values("round_id").dropna(subset=["wifi_rssi_dbm"])
            if g2.empty:
                continue
            ax.plot(g2["round_id"].astype(float), g2["wifi_rssi_dbm"], marker="o", label=tid)
        ax.set_xlabel("Round ID")
        ax.set_ylabel("WiFi RSSI (dBm)")
        ax.set_title("WiFi Signal Strength per Terminal per Round")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        ax.invert_yaxis()  # lower dBm = weaker
        fig.tight_layout()
        p = run_dir / "plot_wifi_rssi.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    # RTT & Bandwidth (from satisfaction_meta)
    has_rtt = "sat_rtt_ms" in idx_df.columns and idx_df["sat_rtt_ms"].notna().any()
    has_bw = "sat_bandwidth_mbps" in idx_df.columns and idx_df["sat_bandwidth_mbps"].notna().any()
    if has_rtt or has_bw:
        n_axes = sum([has_rtt, has_bw])
        fig, axes = plt.subplots(n_axes, 1, figsize=(10, 5 * n_axes), sharex=True)
        if n_axes == 1:
            axes = [axes]
        ax_idx = 0
        if has_rtt:
            ax = axes[ax_idx]
            for tid, g in idx_df.groupby("terminal_id"):
                g2 = g.sort_values("round_id").dropna(subset=["sat_rtt_ms"])
                if g2.empty:
                    continue
                ax.plot(g2["round_id"].astype(float), g2["sat_rtt_ms"], marker="v", label=tid)
            ax.set_ylabel("RTT (ms)")
            ax.set_title("Measured RTT per Terminal (from Satisfaction Meta)")
            ax.legend(fontsize=8)
            ax.grid(True, alpha=0.3)
            ax_idx += 1
        if has_bw:
            ax = axes[ax_idx]
            for tid, g in idx_df.groupby("terminal_id"):
                g2 = g.sort_values("round_id").dropna(subset=["sat_bandwidth_mbps"])
                if g2.empty:
                    continue
                ax.plot(g2["round_id"].astype(float), g2["sat_bandwidth_mbps"], marker="D", label=tid)
            ax.set_ylabel("Bandwidth (Mbps)")
            ax.set_title("Measured Bandwidth per Terminal")
            ax.legend(fontsize=8)
            ax.grid(True, alpha=0.3)
        axes[-1].set_xlabel("Round ID")
        fig.tight_layout()
        p = run_dir / "plot_network_rtt_bandwidth.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    return names


def write_resource_plots(idx_df, df, run_dir: Path) -> List[str]:
    """Battery level, memory usage, storage from rich telemetry."""
    plt = _setup_matplotlib()
    names = []

    # Use idx_df (richer) if available, fallback to df
    src = idx_df if idx_df is not None and not idx_df.empty else df

    # Battery trend (battery_level from index.jsonl or battery_at_start/end from training)
    has_battery_level = "battery_level" in src.columns and src["battery_level"].notna().any()
    has_battery_training = "battery_at_start" in src.columns and src["battery_at_start"].notna().any()

    if has_battery_level or has_battery_training:
        fig, axes = plt.subplots(1 + int(has_battery_training and has_battery_level), 1,
                                 figsize=(10, 5 * (1 + int(has_battery_training and has_battery_level))),
                                 sharex=True, squeeze=False)
        axes = axes.flatten()
        ax_idx = 0

        if has_battery_level:
            ax = axes[ax_idx]
            for tid, g in src.groupby("terminal_id"):
                g2 = g.sort_values("round_id").dropna(subset=["battery_level"])
                if g2.empty:
                    continue
                ax.plot(g2["round_id"].astype(float), g2["battery_level"].astype(float) * 100, marker="o", label=tid)
            ax.set_ylabel("Battery Level (%)")
            ax.set_title("Battery Level Over Rounds (Device Report)")
            ax.legend(fontsize=8)
            ax.grid(True, alpha=0.3)
            ax_idx += 1

        if has_battery_training and (ax_idx < len(axes)):
            ax = axes[ax_idx]
            for tid, g in src.groupby("terminal_id"):
                g2 = g.sort_values("round_id").dropna(subset=["battery_at_start", "battery_at_end"])
                if g2.empty:
                    continue
                drop = g2["battery_at_start"].astype(float) - g2["battery_at_end"].astype(float)
                ax.bar(g2["round_id"].astype(float) + list(src["terminal_id"].unique()).index(tid) * 0.2 - 0.3,
                       drop, width=0.2, alpha=0.7, label=f"{tid} drain")
            ax.set_ylabel("Battery Drain per Round (%)")
            ax.set_title("Battery Consumed During Training")
            ax.legend(fontsize=8)
            ax.grid(True, alpha=0.3, axis="y")

        axes[-1].set_xlabel("Round ID")
        fig.tight_layout()
        p = run_dir / "plot_battery.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    # Memory usage
    has_mem = ("mem_used_start" in src.columns and src["mem_used_start"].notna().any()) or \
              ("process_rss_mb" in src.columns and src["process_rss_mb"].notna().any())
    if has_mem:
        fig, ax = plt.subplots(figsize=(10, 5))
        for tid, g in src.groupby("terminal_id"):
            g2 = g.sort_values("round_id")
            if "process_rss_mb" in g2.columns and g2["process_rss_mb"].notna().any():
                g3 = g2.dropna(subset=["process_rss_mb"])
                ax.plot(g3["round_id"].astype(float), g3["process_rss_mb"], marker="s", label=f"{tid} RSS")
            elif "mem_used_end" in g2.columns and g2["mem_used_end"].notna().any():
                g3 = g2.dropna(subset=["mem_used_end"])
                ax.plot(g3["round_id"].astype(float), g3["mem_used_end"], marker="s", label=f"{tid} Mem End")
        ax.set_xlabel("Round ID")
        ax.set_ylabel("Memory (MB)")
        ax.set_title("Process Memory Usage Over Rounds")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        p = run_dir / "plot_memory_usage.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    return names


def write_upload_timing_plots(rep: TrialReport, run_dir: Path) -> List[str]:
    """Upload latency from event logs (weights_receive_start to manifest_saved)."""
    plt = _setup_matplotlib()
    names = []
    udf = _upload_timing_dataframe(rep)
    if udf is None or udf.empty:
        return names

    fig, ax = plt.subplots(figsize=(10, 5))
    terminals = sorted(udf["terminal_id"].unique())
    width = 0.8 / max(len(terminals), 1)
    for i, tid in enumerate(terminals):
        g = udf[udf["terminal_id"] == tid].sort_values("round_id")
        offset = (i - len(terminals) / 2 + 0.5) * width
        ax.bar(g["round_id"].astype(float) + offset, g["upload_duration_ms"], width=width, alpha=0.8, label=tid)
    ax.set_xlabel("Round ID")
    ax.set_ylabel("Upload Duration (ms)")
    ax.set_title("Weight Upload Latency (receive_start -> manifest_saved)")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3, axis="y")
    fig.tight_layout()
    p = run_dir / "plot_upload_latency.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    names.append(p.name)
    return names


def write_edge_aggregation_plots(rep: TrialReport, run_dir: Path) -> List[str]:
    """Edge aggregation stats: num_clients and sum_n_samples per round."""
    plt = _setup_matplotlib()
    names = []
    edf = _edge_agg_dataframe(rep)
    if edf is None or edf.empty:
        return names

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    edges = sorted(edf["edge_id"].unique())

    for eid in edges:
        g = edf[edf["edge_id"] == eid].sort_values("round")
        if g["num_clients"].notna().any():
            ax1.plot(g["round"].astype(float), g["num_clients"], marker="o", label=eid)
        if g["sum_n_samples"].notna().any():
            ax2.plot(g["round"].astype(float), g["sum_n_samples"], marker="s", label=eid)

    ax1.set_ylabel("Number of Clients")
    ax1.set_title("Edge Server Aggregation: Client Participation per Round")
    ax1.legend(fontsize=8)
    ax1.grid(True, alpha=0.3)
    ax2.set_ylabel("Total Samples")
    ax2.set_xlabel("Round")
    ax2.set_title("Total Training Samples Aggregated per Round")
    ax2.legend(fontsize=8)
    ax2.grid(True, alpha=0.3)
    fig.tight_layout()
    p = run_dir / "plot_edge_aggregation.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    names.append(p.name)
    return names


def write_app_type_plots(df, idx_df, run_dir: Path) -> List[str]:
    """App-type analysis: satisfaction by app, data samples by app."""
    plt = _setup_matplotlib()
    import numpy as np
    names = []

    src = idx_df if idx_df is not None and not idx_df.empty else df
    if src is None or src.empty:
        return names

    has_app = "app_type" in src.columns and src["app_type"].notna().any()
    if not has_app:
        return names

    # Satisfaction by app type (grouped bar)
    has_sat = src["satisfaction_before"].notna().any()
    if has_sat:
        fig, ax = plt.subplots(figsize=(10, 6))
        app_types = sorted(src["app_type"].dropna().unique())
        x = np.arange(len(app_types))
        bef_vals = []
        aft_vals = []
        for app in app_types:
            grp = src[src["app_type"] == app]
            bef_vals.append(grp["satisfaction_before"].mean())
            aft_vals.append(grp["satisfaction_after"].mean() if "satisfaction_after" in grp.columns else float("nan"))
        w = 0.35
        ax.bar(x - w / 2, bef_vals, w, label="Before", color="steelblue", alpha=0.8)
        ax.bar(x + w / 2, aft_vals, w, label="After", color="coral", alpha=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(app_types, rotation=30, ha="right")
        ax.set_ylabel("Satisfaction (mean)")
        ax.set_title("QoS Satisfaction by Application Type")
        ax.set_ylim(0, 1.1)
        ax.legend()
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        p = run_dir / "plot_satisfaction_by_app.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    # Accuracy by app type
    has_acc = "accuracy" in src.columns and src["accuracy"].notna().any()
    if has_acc:
        fig, ax = plt.subplots(figsize=(10, 6))
        app_types = sorted(src["app_type"].dropna().unique())
        for app in app_types:
            grp = src[src["app_type"] == app]
            for tid, g in grp.groupby("terminal_id"):
                g2 = g.sort_values("round_id").dropna(subset=["accuracy"])
                if g2.empty:
                    continue
                ax.plot(g2["round_id"].astype(float), g2["accuracy"], marker="o", label=f"{tid} ({app})")
        ax.set_xlabel("Round ID")
        ax.set_ylabel("Accuracy")
        ax.set_title("Accuracy Trends by Application Type")
        ax.legend(fontsize=7, loc="lower right")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        p = run_dir / "plot_accuracy_by_app.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    # Data samples per app type (bar)
    has_samples = "data_samples_count" in src.columns and src["data_samples_count"].notna().any()
    if has_samples:
        fig, ax = plt.subplots(figsize=(8, 5))
        app_types = sorted(src["app_type"].dropna().unique())
        x = np.arange(len(app_types))
        means = [src[src["app_type"] == app]["data_samples_count"].mean() for app in app_types]
        ax.bar(x, means, color="mediumpurple", alpha=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(app_types, rotation=30, ha="right")
        ax.set_ylabel("Avg Data Samples")
        ax.set_title("Average Training Data Samples by App Type")
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        p = run_dir / "plot_samples_by_app.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        names.append(p.name)

    return names


def write_weight_norm_latency_plots(df, run_dir: Path) -> List[str]:
    plt = _setup_matplotlib()
    names = []

    has_norm = "weight_norm" in df.columns and df["weight_norm"].notna().any()
    has_lat = "request_duration_ms" in df.columns and df["request_duration_ms"].notna().any()
    if not has_norm and not has_lat:
        return names

    n_axes = sum([has_norm, has_lat])
    fig, axes = plt.subplots(n_axes, 1, figsize=(10, 4 * n_axes), sharex=True, squeeze=False)
    axes = axes.flatten()
    ax_idx = 0

    if has_norm:
        ax = axes[ax_idx]
        for tid, g in df.groupby("terminal_id"):
            g2 = g.sort_values("round_id").dropna(subset=["weight_norm"])
            if g2.empty:
                continue
            ax.plot(g2["round_id"].astype(float), g2["weight_norm"], marker="o", label=f"{tid}")
        ax.set_ylabel("Weight Norm (L2)")
        ax.set_title("Model Update Magnitude (Weight Norm)")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        ax_idx += 1

    if has_lat:
        ax = axes[ax_idx]
        for tid, g in df.groupby("terminal_id"):
            g2 = g.sort_values("round_id").dropna(subset=["request_duration_ms"])
            if g2.empty:
                continue
            ax.plot(g2["round_id"].astype(float), g2["request_duration_ms"], marker="v", linestyle=":", label=f"{tid}")
        ax.set_ylabel("Request Latency (ms)")
        ax.set_title("Server Request Latency per Round")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

    axes[-1].set_xlabel("Round ID")
    fig.tight_layout()
    p = run_dir / "plot_weight_norm_latency.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    names.append(p.name)
    return names


def write_latency_vs_satisfaction_plot(df, idx_df, rep, run_dir: Path) -> List[str]:
    """Scatter: communication load (RTT / upload latency) vs satisfaction."""
    plt = _setup_matplotlib()
    import numpy as np
    names = []

    scatter_rows = []
    # Upload latency
    udf = _upload_timing_dataframe(rep)
    if udf is not None and not udf.empty:
        merged = df.merge(udf[["terminal_id", "round_id", "upload_duration_ms"]],
                          on=["terminal_id", "round_id"], how="left")
    else:
        merged = df.copy()
        merged["upload_duration_ms"] = None

    # RTT from idx_df
    if idx_df is not None and not idx_df.empty and "sat_rtt_ms" in idx_df.columns:
        rtt_src = idx_df.dropna(subset=["sat_rtt_ms"])
        if not rtt_src.empty:
            rtt_map = {}
            for _, r in rtt_src.iterrows():
                rtt_map[(r["terminal_id"], r["round_id"])] = r["sat_rtt_ms"]
            merged["rtt_ms"] = merged.apply(
                lambda r: rtt_map.get((r["terminal_id"], r["round_id"])), axis=1)
        else:
            merged["rtt_ms"] = None
    else:
        merged["rtt_ms"] = None

    for _, row in merged.iterrows():
        sat = _safe_float(row.get("satisfaction_after"), row.get("satisfaction_before"))
        if sat is None:
            continue
        ul = row.get("upload_duration_ms")
        rtt = row.get("rtt_ms")
        tid = row.get("terminal_id", "")
        if ul is not None and not (isinstance(ul, float) and ul != ul):
            scatter_rows.append({"type": "Upload Latency (ms)", "latency": float(ul),
                                 "satisfaction": float(sat), "terminal": tid})
        if rtt is not None and not (isinstance(rtt, float) and rtt != rtt):
            scatter_rows.append({"type": "RTT (ms)", "latency": float(rtt),
                                 "satisfaction": float(sat), "terminal": tid})

    if not scatter_rows:
        return names

    import pandas as pd
    sdf = pd.DataFrame(scatter_rows)
    latency_types = sdf["type"].unique()
    fig, axes = plt.subplots(1, len(latency_types), figsize=(7 * len(latency_types), 6), squeeze=False)
    axes = axes.flatten()
    for ax, lt in zip(axes, latency_types):
        sub = sdf[sdf["type"] == lt]
        ax.scatter(sub["latency"], sub["satisfaction"], alpha=0.6, edgecolors="k", linewidth=0.3, s=60)
        if len(sub) >= 3:
            z = np.polyfit(sub["latency"], sub["satisfaction"], 1)
            px = np.linspace(sub["latency"].min(), sub["latency"].max(), 50)
            ax.plot(px, np.polyval(z, px), color="red", linewidth=2, linestyle="--",
                    label=f"trend (slope={z[0]:.4f})")
            ax.legend(fontsize=9)
        ax.set_xlabel(lt)
        ax.set_ylabel("Satisfaction")
        ax.set_title(f"Communication Load vs Satisfaction")
        ax.grid(True, alpha=0.3)
    fig.suptitle("Communication Load Impact on QoS Satisfaction", fontsize=13, fontweight="bold")
    fig.tight_layout()
    p = run_dir / "plot_latency_vs_satisfaction.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    names.append(p.name)
    return names


def write_device_summary_plot(idx_df, run_dir: Path) -> List[str]:
    """Device info summary table as a figure."""
    plt = _setup_matplotlib()
    names = []
    if idx_df is None or idx_df.empty:
        return names

    # Build summary per terminal
    summary_rows = []
    for tid, g in idx_df.groupby("terminal_id"):
        row = {
            "Terminal": tid,
            "Device": f"{g['device_manufacturer'].iloc[0] or ''} {g['device_model'].iloc[0] or ''}".strip(),
            "OS SDK": str(g["os_sdk_int"].iloc[0] or ""),
            "Rounds": str(g["round_id"].nunique()),
            "Avg Acc": f"{g['accuracy'].mean():.3f}" if g["accuracy"].notna().any() else "-",
            "Avg Loss": f"{g['loss'].mean():.3f}" if g["loss"].notna().any() else "-",
            "Avg RSSI": f"{g['wifi_rssi_dbm'].mean():.0f}" if g["wifi_rssi_dbm"].notna().any() else "-",
            "Avg RTT(ms)": f"{g['sat_rtt_ms'].mean():.0f}" if g["sat_rtt_ms"].notna().any() else "-",
            "Avg BW(Mbps)": f"{g['sat_bandwidth_mbps'].mean():.1f}" if g["sat_bandwidth_mbps"].notna().any() else "-",
        }
        summary_rows.append(row)

    if not summary_rows:
        return names

    fig, ax = plt.subplots(figsize=(14, 1 + 0.5 * len(summary_rows)))
    ax.axis("off")
    cols = list(summary_rows[0].keys())
    cell_text = [[r[c] for c in cols] for r in summary_rows]
    table = ax.table(cellText=cell_text, colLabels=cols, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.4)
    # Header styling
    for j in range(len(cols)):
        table[(0, j)].set_facecolor("#4472C4")
        table[(0, j)].set_text_props(color="white", fontweight="bold")
    ax.set_title("Device & Performance Summary", fontsize=12, fontweight="bold", pad=20)
    fig.tight_layout()
    p = run_dir / "plot_device_summary.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    names.append(p.name)
    return names


def write_convergence_overview_plot(df, idx_df, run_dir: Path) -> List[str]:
    """Multi-panel convergence overview: Acc, Loss, Satisfaction, Training Time."""
    plt = _setup_matplotlib()
    names = []

    src = df
    if src is None or src.empty:
        return names

    # Need at least accuracy to make this useful
    if "accuracy" not in src.columns or not src["accuracy"].notna().any():
        return names

    panels = []
    if src["accuracy"].notna().any():
        panels.append(("accuracy", "Accuracy", False))
    if "loss" in src.columns and src["loss"].notna().any():
        panels.append(("loss", "Loss", False))
    if src["satisfaction_before"].notna().any():
        panels.append(("satisfaction_before", "Satisfaction (Before)", False))
    if "total_local_ms" in src.columns and src["total_local_ms"].notna().any():
        panels.append(("total_local_ms", "Training Time (ms)", False))

    if len(panels) < 2:
        return names

    fig, axes = plt.subplots(len(panels), 1, figsize=(12, 3.5 * len(panels)), sharex=True)
    if len(panels) == 1:
        axes = [axes]

    rids = sorted(src["round_id"].dropna().unique())

    for ax, (col, title, _) in zip(axes, panels):
        # Per-terminal lines
        for tid, g in src.groupby("terminal_id"):
            g2 = g.sort_values("round_id").dropna(subset=[col])
            if g2.empty:
                continue
            ax.plot(g2["round_id"].astype(float), g2[col].astype(float), marker="o", markersize=4, label=tid, alpha=0.7)
        # System mean
        means = []
        for r in rids:
            vals = src[src["round_id"] == r][col].dropna()
            means.append(vals.mean() if len(vals) > 0 else float("nan"))
        ax.plot(rids, means, color="black", linewidth=2.5, linestyle="--", marker="D", markersize=6, label="System Mean", zorder=5)
        ax.set_ylabel(title)
        ax.set_title(title)
        ax.legend(fontsize=7, loc="best")
        ax.grid(True, alpha=0.3)

    axes[-1].set_xlabel("Round ID")
    fig.suptitle("System Convergence Overview", fontsize=14, fontweight="bold", y=1.01)
    fig.tight_layout()
    p = run_dir / "plot_convergence_overview.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    names.append(p.name)
    return names


def write_ap_distribution_plots(df, run_dir: Path) -> List[str]:
    plt = _setup_matplotlib()
    names = []
    if "predicted_ap" not in df.columns or not df["predicted_ap"].notna().any():
        return names

    fig, ax = plt.subplots(figsize=(6, 6))
    counts = df["predicted_ap"].value_counts()
    labels = [f"AP_{int(i)}" for i in counts.index]
    ax.pie(counts, labels=labels, autopct="%1.1f%%", startangle=90, colors=plt.cm.Paired.colors)
    ax.set_title("Overall AP Selection Distribution")
    fig.tight_layout()
    p = run_dir / "plot_ap_distribution.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    names.append(p.name)
    return names


def write_central_timing_plots(rep: TrialReport, run_dir: Path) -> List[str]:
    """Central server event timeline."""
    plt = _setup_matplotlib()
    names = []
    if not rep.central_timing:
        return names

    events_by_type = defaultdict(list)
    for ev in rep.central_timing:
        ts = _parse_ts(ev.get("ts", ""))
        if ts:
            events_by_type[ev.get("event", "unknown")].append(ts)

    if not events_by_type:
        return names

    fig, ax = plt.subplots(figsize=(12, 4))
    colors = {"aggregation_completed": "green", "model_received": "blue", "request_e2e": "orange"}
    for i, (ev_type, timestamps) in enumerate(sorted(events_by_type.items())):
        for ts in timestamps:
            ax.scatter(ts, i, color=colors.get(ev_type, "gray"), s=80, zorder=3)
        ax.scatter([], [], color=colors.get(ev_type, "gray"), s=80, label=f"{ev_type} ({len(timestamps)})")

    ax.set_yticks(range(len(events_by_type)))
    ax.set_yticklabels(sorted(events_by_type.keys()), fontsize=9)
    ax.set_xlabel("Time")
    ax.set_title("Central Server Event Timeline")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3, axis="x")
    fig.autofmt_xdate()
    fig.tight_layout()
    p = run_dir / "plot_central_timeline.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    names.append(p.name)
    return names


def write_epoch_distribution_plots(idx_df, run_dir: Path) -> List[str]:
    """Epoch duration distribution per terminal (box plot)."""
    plt = _setup_matplotlib()
    names = []
    if idx_df is None or idx_df.empty:
        return names
    if "total_local_ms" not in idx_df.columns or not idx_df["total_local_ms"].notna().any():
        return names

    terminals = sorted(idx_df["terminal_id"].dropna().unique())
    data = []
    labels = []
    for tid in terminals:
        vals = idx_df[idx_df["terminal_id"] == tid]["total_local_ms"].dropna().tolist()
        if vals:
            data.append(vals)
            labels.append(tid)

    if not data:
        return names

    fig, ax = plt.subplots(figsize=(8, 5))
    bp = ax.boxplot(data, tick_labels=labels, patch_artist=True)
    cmap = plt.get_cmap("tab10")
    for i, patch in enumerate(bp["boxes"]):
        patch.set_facecolor(cmap(i % 10))
        patch.set_alpha(0.6)
    ax.set_ylabel("Total Local Training (ms)")
    ax.set_title("Training Duration Distribution by Terminal")
    ax.grid(True, alpha=0.3, axis="y")
    fig.tight_layout()
    p = run_dir / "plot_training_distribution.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    names.append(p.name)
    return names


def write_data_samples_plot(df, idx_df, run_dir: Path) -> List[str]:
    """Data samples count per terminal per round."""
    plt = _setup_matplotlib()
    names = []
    src = idx_df if idx_df is not None and not idx_df.empty else df
    if src is None or src.empty:
        return names
    if "data_samples_count" not in src.columns or not src["data_samples_count"].notna().any():
        return names

    fig, ax = plt.subplots(figsize=(10, 5))
    for tid, g in src.groupby("terminal_id"):
        g2 = g.sort_values("round_id").dropna(subset=["data_samples_count"])
        if g2.empty:
            continue
        ax.plot(g2["round_id"].astype(float), g2["data_samples_count"], marker="o", label=tid)
    ax.set_xlabel("Round ID")
    ax.set_ylabel("Data Samples Count")
    ax.set_title("Training Data Samples per Terminal per Round")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    p = run_dir / "plot_data_samples.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    names.append(p.name)
    return names


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------
def build_markdown(rep: TrialReport, plot_filenames: Optional[List[str]] = None) -> str:
    lines: List[str] = []
    lines.append("# Trial Analysis Report")
    lines.append("")
    lines.append(f"- Central log: `{rep.central_path.relative_to(ROOT)}`")
    lines.append(f"- Time window: `{rep.window_start}` - `{rep.window_end}`")
    lines.append(f"- Generated: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`")
    lines.append("")

    # Data source counts
    lines.append("## Data Sources")
    lines.append(f"| Source | Count |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Telemetry rows | {len(rep.telemetry_rows)} |")
    lines.append(f"| Weights received | {len(rep.weights_received_rows)} |")
    lines.append(f"| Manifests | {len(rep.manifests)} |")
    lines.append(f"| Index telemetry (rich) | {len(rep.index_telemetry)} |")
    lines.append(f"| Upload events | {len(rep.upload_events)} |")
    lines.append(f"| Edge aggregation meta | {len(rep.edge_agg_meta)} |")
    lines.append(f"| Central timing events | {len(rep.central_timing)} |")
    lines.append(f"| Post-switch telemetry | {len(rep.post_switch_telemetry)} |")
    lines.append(f"| Edge updates | {len(rep.edge_updates)} |")
    lines.append(f"| Aggregation events | {len({json.dumps(x, sort_keys=True) for x in rep.aggregations})} |")
    lines.append("")

    # Aggregation summary
    lines.append("## Global Aggregation Events")
    seen = set()
    for a in rep.aggregations:
        key = json.dumps(a, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {a}")
    lines.append("")

    # Edge updates
    lines.append("## Edge Updates (excerpt)")
    for u in rep.edge_updates[:30]:
        lines.append(f"- edge_id={u.get('edge_id')} round={u.get('round')} run_id={u.get('run_id')}")
    if len(rep.edge_updates) > 30:
        lines.append(f"- ... {len(rep.edge_updates) - 30} more")
    lines.append("")

    # Terminal telemetry detail
    lines.append("## Terminal Telemetry Detail")
    by_term: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rep.telemetry_rows:
        tid = row.get("terminal_id") or row.get("edge_id") or "unknown"
        cm = _client_meta_dict(row.get("client_meta"))
        sat_b = row.get("satisfaction_before") or cm.get("satisfaction_before")
        sat_a = row.get("satisfaction_after") or cm.get("satisfaction_after") or row.get("terminal_satisfaction_after")
        by_term[tid].append({
            "round_id": row.get("round_id") or row.get("round"),
            "cycle": cm.get("cycle_number"),
            "app_type": cm.get("app_type") or row.get("app_type"),
            "accuracy": _safe_float(row.get("accuracy"), cm.get("accuracy")),
            "loss": _safe_float(row.get("loss"), cm.get("loss")),
            "sat_before": _safe_float(sat_b),
            "sat_after": _safe_float(sat_a),
            "total_ms": _safe_float(cm.get("total_local_ms")),
        })
    for tid in sorted(by_term.keys()):
        rows = sorted(by_term[tid], key=lambda x: (x.get("round_id") or 0, x.get("cycle") or 0))
        lines.append(f"### {tid}")
        lines.append("| Round | Cycle | App | Acc | Loss | Sat(B) | Sat(A) | Time(ms) |")
        lines.append("|-------|-------|-----|-----|------|--------|--------|----------|")
        for r in rows:
            acc_s = f"{r['accuracy']:.3f}" if r.get("accuracy") is not None else "-"
            loss_s = f"{r['loss']:.3f}" if r.get("loss") is not None else "-"
            sb_s = f"{r['sat_before']:.2f}" if r.get("sat_before") is not None else "-"
            sa_s = f"{r['sat_after']:.2f}" if r.get("sat_after") is not None else "-"
            ms_s = f"{r['total_ms']:.0f}" if r.get("total_ms") is not None else "-"
            lines.append(f"| {r.get('round_id', '-')} | {r.get('cycle', '-')} | {r.get('app_type', '-')} | {acc_s} | {loss_s} | {sb_s} | {sa_s} | {ms_s} |")
        lines.append("")

    # Satisfaction summary
    sat_records = []
    for tid, rows in by_term.items():
        for r in rows:
            if (r.get("sat_before") is not None or r.get("sat_after") is not None) and r.get("round_id") is not None:
                sat_records.append({"terminal_id": tid, "round_id": r["round_id"],
                                    "sat_before": r.get("sat_before"), "sat_after": r.get("sat_after")})
    if sat_records:
        lines.append("## Satisfaction Summary by Round")
        by_rnd: Dict[Any, list] = defaultdict(list)
        for rec in sat_records:
            by_rnd[rec["round_id"]].append(rec)
        lines.append("| Round | Terminals | Avg Before | HM Before | Avg After | HM After | Min After |")
        lines.append("|-------|-----------|------------|-----------|-----------|----------|-----------|")
        for rnd in sorted(by_rnd.keys()):
            recs = by_rnd[rnd]
            befs = [r["sat_before"] for r in recs if r.get("sat_before") is not None]
            afts = [r["sat_after"] for r in recs if r.get("sat_after") is not None]
            bef_avg = sum(befs) / len(befs) if befs else None
            aft_avg = sum(afts) / len(afts) if afts else None
            aft_min = min(afts) if afts else None
            hm_bef = _harmonic_mean(befs)
            hm_aft = _harmonic_mean(afts)
            lines.append(f"| {rnd} | {len(recs)} | {f'{bef_avg*100:.1f}%' if bef_avg else '-'} | "
                         f"{f'{hm_bef*100:.1f}%' if not math.isnan(hm_bef) else '-'} | "
                         f"{f'{aft_avg*100:.1f}%' if aft_avg else '-'} | "
                         f"{f'{hm_aft*100:.1f}%' if not math.isnan(hm_aft) else '-'} | "
                         f"{f'{aft_min*100:.1f}%' if aft_min else '-'} |")
        lines.append("")

    # Edge aggregation summary
    if rep.edge_agg_meta:
        lines.append("## Edge Aggregation Metadata")
        lines.append("| Round | Edge | Clients | Samples | Run ID |")
        lines.append("|-------|------|---------|---------|--------|")
        for meta in sorted(rep.edge_agg_meta, key=lambda m: (m.get("round", 0) or m.get("_round_from_path", 0), m.get("edge_id", ""))):
            rnd = meta.get("round") or meta.get("_round_from_path", "-")
            eid = meta.get("edge_id") or meta.get("_edge_from_path", "-")
            nc = meta.get("num_clients", "-")
            ns = meta.get("sum_n_samples", "-")
            rid = (meta.get("run_id") or meta.get("federation_run_id") or "-")[:20]
            lines.append(f"| {rnd} | {eid} | {nc} | {ns} | {rid}... |")
        lines.append("")

    # Plots
    if plot_filenames:
        lines.append("## Generated Plots")
        for fn in plot_filenames:
            lines.append(f"![{fn}]({fn})")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Group Comparison Mode
# ---------------------------------------------------------------------------
def _run_group_comparison(central_logs: List[str], out_dir: str, no_plots: bool, min_rounds: int) -> int:
    """
    複数試行のログを読み込み、グループ間比較レポートを生成する。
    各 central log に対して簡易的な指標抽出を行い、比較テーブルと差分グラフを出力。
    """
    base_out = (ROOT / out_dir).resolve()
    base_out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    group_dir = base_out / f"group_comparison_{stamp}"
    group_dir.mkdir(parents=True, exist_ok=True)

    summaries: List[Dict[str, Any]] = []

    for log_str in central_logs:
        log_path = Path(log_str)
        if not log_path.is_absolute():
            log_path = (ROOT / log_path).resolve()
        if not log_path.is_file():
            print(f"WARN: {log_path} not found, skipping.", file=sys.stderr)
            continue

        rep = TrialReport(central_path=log_path)
        parse_central(log_path, rep)
        window = (rep.window_start, rep.window_end)
        jsonl_files = collect_round_and_edge_logs(window)
        for p in jsonl_files:
            ingest_jsonl_file(p, rep, window)
        rep.post_switch_telemetry = load_post_switch_telemetry(window)
        rep.index_telemetry = load_full_index_telemetry(window)
        _enrich_telemetry_from_index(rep)

        # Extract summary metrics
        sat_afters = [r.get("satisfaction_after") for r in rep.telemetry_rows
                      if r.get("satisfaction_after") is not None]
        sat_befores = [r.get("satisfaction_before") for r in rep.telemetry_rows
                       if r.get("satisfaction_before") is not None]
        training_times = [r.get("total_local_ms") for r in rep.telemetry_rows
                          if r.get("total_local_ms") is not None]

        eps = 1e-9

        def _hm(vals):
            if not vals:
                return None
            return len(vals) / sum(1.0 / max(v, eps) for v in vals)

        summary = {
            "log": log_path.name,
            "window_start": rep.window_start.isoformat() if rep.window_start else "?",
            "window_end": rep.window_end.isoformat() if rep.window_end else "?",
        }

        # Filter by minimum rounds
        n_rounds = len({r.get("round_id") or r.get("round") for r in rep.telemetry_rows if (r.get("round_id") or r.get("round")) is not None})
        if n_rounds < min_rounds:
            print(f"WARN: Skipping {log_path.name} (only {n_rounds} rounds completed, requires {min_rounds})", file=sys.stderr)
            continue
            
        summary["n_rounds"] = n_rounds
        summary["n_telemetry"] = len(rep.telemetry_rows)
        summary["sat_before_avg"] = float(sum(sat_befores) / len(sat_befores)) if sat_befores else None
        summary["sat_before_hm"] = _hm(sat_befores)
        summary["sat_after_avg"] = float(sum(sat_afters) / len(sat_afters)) if sat_afters else None
        summary["sat_after_hm"] = _hm(sat_afters)
        summary["sat_delta"] = (float(sum(sat_afters) / len(sat_afters)) - float(sum(sat_befores) / len(sat_befores))) if sat_afters and sat_befores else None
        summary["training_time_avg_ms"] = float(sum(training_times) / len(training_times)) if training_times else None

        summaries.append(summary)

    if not summaries:
        print("No valid logs found for group comparison.", file=sys.stderr)
        return 2

    # ── Compute standard deviation across trials (statistical confidence) ──
    def _std(vals):
        vals = [v for v in vals if v is not None]
        if len(vals) < 2:
            return None
        mean = sum(vals) / len(vals)
        variance = sum((x - mean) ** 2 for x in vals) / (len(vals) - 1)
        return variance ** 0.5

    sat_after_vals = [s["sat_after_avg"] for s in summaries if s.get("sat_after_avg") is not None]
    sat_before_vals = [s["sat_before_avg"] for s in summaries if s.get("sat_before_avg") is not None]
    delta_vals = [s["sat_delta"] for s in summaries if s.get("sat_delta") is not None]
    train_time_vals = [s["training_time_avg_ms"] for s in summaries if s.get("training_time_avg_ms") is not None]

    cross_trial_stats = {
        "n_trials": len(summaries),
        "sat_after_mean": sum(sat_after_vals) / len(sat_after_vals) if sat_after_vals else None,
        "sat_after_std": _std(sat_after_vals),
        "sat_before_mean": sum(sat_before_vals) / len(sat_before_vals) if sat_before_vals else None,
        "sat_before_std": _std(sat_before_vals),
        "sat_delta_mean": sum(delta_vals) / len(delta_vals) if delta_vals else None,
        "sat_delta_std": _std(delta_vals),
        "training_time_mean_ms": sum(train_time_vals) / len(train_time_vals) if train_time_vals else None,
        "training_time_std_ms": _std(train_time_vals),
    }

    # Write comparison markdown
    lines = ["# Group Comparison Report", "", f"Generated: {stamp}", ""]
    lines.append(f"## Trials Compared: {len(summaries)}")
    lines.append("")
    lines.append("| Log | Rounds | Telemetry | Sat_Before(avg) | Sat_Before(HM) | Sat_After(avg) | Sat_After(HM) | Delta | Train_ms(avg) |")
    lines.append("|-----|--------|-----------|-----------------|----------------|----------------|---------------|-------|---------------|")
    for s in summaries:
        def _f(v, pct=False):
            if v is None:
                return "N/A"
            return f"{v*100:.1f}%" if pct else f"{v:.1f}"
        lines.append(
            f"| {s['log'][:30]} | {s['n_rounds']} | {s['n_telemetry']} | "
            f"{_f(s['sat_before_avg'], True)} | {_f(s['sat_before_hm'], True)} | "
            f"{_f(s['sat_after_avg'], True)} | {_f(s['sat_after_hm'], True)} | "
            f"{_f(s['sat_delta'], True)} | {_f(s['training_time_avg_ms'])} |"
        )
    lines.append("")

    # ── Cross-trial standard deviation section ──
    lines.append("## Statistical Confidence (Cross-Trial Standard Deviation)")
    lines.append("")
    lines.append("| Metric | Mean | Std | CV (%) |")
    lines.append("|--------|------|-----|--------|")
    for label, mean_key, std_key in [
        ("Satisfaction After", "sat_after_mean", "sat_after_std"),
        ("Satisfaction Before", "sat_before_mean", "sat_before_std"),
        ("Satisfaction Delta", "sat_delta_mean", "sat_delta_std"),
        ("Training Time (ms)", "training_time_mean_ms", "training_time_std_ms"),
    ]:
        mean_v = cross_trial_stats.get(mean_key)
        std_v = cross_trial_stats.get(std_key)
        cv = (std_v / abs(mean_v) * 100) if mean_v and std_v and abs(mean_v) > 1e-9 else None
        mean_s = f"{mean_v:.4f}" if mean_v is not None else "N/A"
        std_s = f"{std_v:.4f}" if std_v is not None else "N/A"
        cv_s = f"{cv:.1f}" if cv is not None else "N/A"
        lines.append(f"| {label} | {mean_s} | {std_s} | {cv_s} |")
    lines.append("")
    if cross_trial_stats.get("sat_delta_std") is not None and len(delta_vals) >= 2:
        # Report whether the delta is statistically significant (> 2*std from 0)
        mean_d = cross_trial_stats["sat_delta_mean"]
        std_d = cross_trial_stats["sat_delta_std"]
        t_stat = abs(mean_d) / (std_d / (len(delta_vals) ** 0.5)) if std_d > 0 else float("inf")
        sig = "Yes (p<0.05)" if t_stat > 2.0 else "No"
        lines.append(f"**Significance test** (t-stat={t_stat:.2f}, n={len(delta_vals)}): {sig}")
        lines.append("")

    # Best/worst analysis
    if len(summaries) > 1:
        by_delta = sorted([s for s in summaries if s["sat_delta"] is not None], key=lambda x: x["sat_delta"], reverse=True)
        if by_delta:
            lines.append("## Ranking by Satisfaction Delta")
            lines.append("")
            for i, s in enumerate(by_delta, 1):
                lines.append(f"{i}. **{s['log'][:30]}** — delta={s['sat_delta']*100:.1f}%")
            lines.append("")

    md_path = group_dir / "group_comparison.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Save JSON
    json_path = group_dir / "group_comparison.json"
    output_data = {
        "trials": summaries,
        "cross_trial_statistics": cross_trial_stats,
    }
    json_path.write_text(json.dumps(output_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\nGroup comparison: {group_dir}")
    print(f"  Report: {md_path}")
    print(f"  JSON:   {json_path}")
    print(f"  Trials: {len(summaries)}")
    return 0


# ---------------------------------------------------------------------------
# CTrace Analysis Integration
# ---------------------------------------------------------------------------

def _find_latest_trace_dir() -> Optional[Path]:
    """scripts/perfetto/traces/ 配下の最��ディレクトリを返す。
    サブフォル��構成 (before_memory_policy/, after_memory_policy/) に対応。
    """
    traces_root = ROOT / "scripts" / "perfetto" / "traces"
    if not traces_root.exists():
        return None
    # Collect all run directories (potentially nested under policy subdirs)
    candidates: list = []
    for entry in traces_root.iterdir():
        if not entry.is_dir() or entry.name.startswith("verify"):
            continue
        # If entry contains timestamp-named subdirs, it's a policy folder
        subdirs = [d for d in entry.iterdir() if d.is_dir() and not d.name.startswith("verify")]
        if subdirs:
            candidates.extend(subdirs)
        else:
            # Legacy flat structure: entry itself is a run dir
            candidates.append(entry)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.name, reverse=True)
    return candidates[0]


def _parse_ctrace_file(path: Path) -> Dict[str, Any]:
    """CTraceファイルをテキストとしてパースし、sched_switch等のイベントを集計する。"""
    import re as _re
    from collections import Counter as _Counter

    TARGET_APP = ".hfl_experiment"
    stats: Dict[str, Any] = {
        "file": path.name,
        "size_bytes": path.stat().st_size,
        "total_events": 0,
        "sched_switch_count": 0,
        "app_preempted_count": 0,
        "cpu_thieves": {},
        "binder_count": 0,
        "irq_count": 0,
        "total_latency_sec": 0.0,
        "latency_samples": 0,
    }

    if path.stat().st_size < 200:
        stats["error"] = "file too small (likely empty trace)"
        return stats

    event_pattern = _re.compile(r"(\d+\.\d+):\s+([a-zA-Z0-9_]+):")
    thieves: _Counter = _Counter()
    wakeup_time: Optional[float] = None

    try:
        with open(path, "r", errors="ignore") as f:
            for line in f:
                m = event_pattern.search(line)
                if not m:
                    continue
                ts_val = float(m.group(1))
                event_name = m.group(2)
                stats["total_events"] += 1

                if "binder" in event_name.lower() or "binder" in line.lower():
                    stats["binder_count"] += 1

                if "irq_handler_entry" in event_name or "softirq_entry" in event_name:
                    stats["irq_count"] += 1

                if event_name in ("sched_waking", "sched_wakeup") and TARGET_APP in line:
                    wakeup_time = ts_val

                if event_name == "sched_switch":
                    stats["sched_switch_count"] += 1
                    if f"next_comm={TARGET_APP}" in line and wakeup_time is not None:
                        latency = ts_val - wakeup_time
                        if 0 < latency < 1.0:
                            stats["total_latency_sec"] += latency
                            stats["latency_samples"] += 1
                        wakeup_time = None

                    if f"prev_comm={TARGET_APP}" in line:
                        stats["app_preempted_count"] += 1
                        thief_m = _re.search(r"next_comm=([^ ]+)", line)
                        if thief_m:
                            thieves[thief_m.group(1)] += 1
    except Exception as e:
        stats["error"] = str(e)

    stats["cpu_thieves"] = dict(thieves.most_common(10))
    return stats


def analyze_traces(trace_dir: Path) -> List[Dict[str, Any]]:
    """指定ディレクトリのCTraceファイルを全て分析する。"""
    results: List[Dict[str, Any]] = []
    ctrace_files = sorted(trace_dir.glob("*.ctrace"))
    if not ctrace_files:
        return results
    for cf in ctrace_files:
        print(f"  Parsing CTrace: {cf.name}...", file=sys.stderr)
        results.append(_parse_ctrace_file(cf))
    return results


def _build_ctrace_markdown(trace_results: List[Dict[str, Any]], trace_dir: Path) -> str:
    """CTrace分析結果のMarkdownセクションを生成。"""
    lines: List[str] = []
    lines.append("## OS-Level Trace Analysis (atrace)")
    lines.append(f"\nSource: `{trace_dir}`\n")

    valid = [r for r in trace_results if r.get("total_events", 0) > 0]
    empty = [r for r in trace_results if r.get("total_events", 0) == 0]

    if empty:
        lines.append(f"**Warning**: {len(empty)} trace(s) were empty or too small to parse.\n")

    if not valid:
        lines.append("No valid trace data found.\n")
        return "\n".join(lines)

    # Summary table
    lines.append("| Device | Events | Sched Switch | App Preempted | Binder | IRQ | Latency Loss (s) |")
    lines.append("|--------|--------|-------------|---------------|--------|-----|-----------------|")
    for r in valid:
        dev = r["file"].replace("trace_", "").replace(".ctrace", "").split("_")[0]
        lines.append(
            f"| .{dev} | {r['total_events']:,} | {r['sched_switch_count']:,} | "
            f"{r['app_preempted_count']:,} | {r['binder_count']:,} | {r['irq_count']:,} | "
            f"{r['total_latency_sec']:.3f} |"
        )
    lines.append("")

    # CPU Thieves (top preemptors)
    lines.append("### CPU Preemption Ranking (Top Processes)")
    for r in valid:
        dev = r["file"].replace("trace_", "").replace(".ctrace", "").split("_")[0]
        thieves = r.get("cpu_thieves", {})
        if thieves:
            lines.append(f"\n**Device .{dev}**:\n")
            lines.append("| Process | Count |")
            lines.append("|---------|-------|")
            for proc, cnt in list(thieves.items())[:5]:
                lines.append(f"| {proc} | {cnt:,} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Trace-map: auto-map trace folders to central logs
# ---------------------------------------------------------------------------
_TRACE_TS_RE = re.compile(r"(\d{8}_\d{6})$")


def _parse_folder_ts(name: str) -> Optional[datetime]:
    m = _TRACE_TS_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _build_trace_to_central_map(trace_dir: Path) -> List[Tuple[Path, Path, datetime, datetime]]:
    """Return list of (trace_folder, central_log, trace_ts, log_ts) sorted by trace_ts."""
    logs_dir = ROOT / "logs" / "time_records"

    # Collect all central logs with their timestamps
    central_logs: List[Tuple[datetime, Path]] = []
    for p in sorted(logs_dir.glob("central_server_*.log")):
        # Extract timestamp from filename: central_server_YYYYMMDD_HHMMSS.log
        stem = p.stem  # central_server_20260506_171953
        ts_part = stem.replace("central_server_", "")
        try:
            log_ts = datetime.strptime(ts_part, "%Y%m%d_%H%M%S")
            central_logs.append((log_ts, p))
        except ValueError:
            continue
    central_logs.sort(key=lambda x: x[0])

    if not central_logs:
        return []

    # Collect trace folders with their timestamps
    trace_folders: List[Tuple[datetime, Path]] = []
    for d in sorted(trace_dir.iterdir()):
        if not d.is_dir():
            continue
        ts = _parse_folder_ts(d.name)
        if ts:
            trace_folders.append((ts, d))
    trace_folders.sort(key=lambda x: x[0])

    # Map each trace folder to the closest preceding central log.
    # Trace folder timestamp = trial end (conductor stop).
    # Central log timestamp = server start time.
    # So the matching log must have started BEFORE the trace folder timestamp.
    result: List[Tuple[Path, Path, datetime, datetime]] = []
    for trace_ts, trace_path in trace_folders:
        best_log: Optional[Tuple[datetime, Path]] = None
        for log_ts, log_path in central_logs:
            if log_ts <= trace_ts:
                best_log = (log_ts, log_path)
            else:
                break
        if best_log:
            result.append((trace_path, best_log[1], trace_ts, best_log[0]))

    return result


def _run_trace_map(args) -> int:
    """Run single-trial analysis for each trace folder mapped to its central log."""
    trace_dir = Path(args.trace_map)
    if not trace_dir.is_absolute():
        trace_dir = (ROOT / trace_dir).resolve()
    if not trace_dir.exists():
        print(f"ERROR: Trace directory not found: {trace_dir}", file=sys.stderr)
        return 2

    mapping = _build_trace_to_central_map(trace_dir)
    if not mapping:
        print("ERROR: No trace folders could be mapped to central logs.", file=sys.stderr)
        return 2

    # Print mapping table
    print("=" * 72, file=sys.stderr)
    print("Trace → Central Log Mapping", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print(f"{'Trace Folder':<28} {'Central Log':<45} {'Δ'}", file=sys.stderr)
    print("-" * 72, file=sys.stderr)
    for trace_path, log_path, trace_ts, log_ts in mapping:
        delta = trace_ts - log_ts
        print(f"{trace_path.name:<28} {log_path.name:<45} {delta}", file=sys.stderr)
    print("=" * 72, file=sys.stderr)

    # Run analysis for each
    base_out = (ROOT / args.out_dir).resolve()
    base_out.mkdir(parents=True, exist_ok=True)

    success_count = 0
    skip_count = 0
    for trace_path, log_path, trace_ts, log_ts in mapping:
        print(f"\n{'─' * 60}", file=sys.stderr)
        print(f"Analyzing: {trace_path.name} → {log_path.name}", file=sys.stderr)

        rep = TrialReport(central_path=log_path)
        parse_central(log_path, rep)
        window = (rep.window_start, rep.window_end)

        # Ingest data
        jsonl_files = collect_round_and_edge_logs(window)
        for p in jsonl_files:
            ingest_jsonl_file(p, rep, window)
        rep.manifests = load_manifests_for_window(window, args.manifest_glob)
        rep.post_switch_telemetry = load_post_switch_telemetry(window)
        rep.index_telemetry = load_full_index_telemetry(window)
        _enrich_telemetry_from_index(rep)
        rep.upload_events = load_upload_events(window)
        rep.edge_agg_meta = load_edge_agg_meta(window)

        # Build DataFrames
        df = _telemetry_dataframe(rep)
        idx_df = _index_telemetry_dataframe(rep)

        n_rounds = 0
        if df is not None and "round_id" in df.columns:
            n_rounds = df["round_id"].nunique()

        if n_rounds < args.min_rounds:
            print(f"  SKIP: Only {n_rounds} rounds (min={args.min_rounds})", file=sys.stderr)
            skip_count += 1
            continue

        # Output directory named after trace folder
        run_dir = base_out / f"trace_{trace_path.name}"
        run_dir.mkdir(parents=True, exist_ok=True)

        # Plots
        plot_files: List[str] = []
        if not args.no_plots:
            try:
                _setup_matplotlib()
            except ImportError:
                pass
            else:
                if df is not None and not df.empty:
                    df_plot = df.dropna(subset=["round_id"])
                    if not df_plot.empty:
                        plot_files += write_accuracy_loss_plots(df_plot, run_dir)
                        plot_files += write_training_time_plots(df_plot, run_dir)
                        plot_files += write_satisfaction_plots(df_plot, rep, run_dir)
                        plot_files += write_weight_norm_latency_plots(df_plot, run_dir)
                        plot_files += write_ap_distribution_plots(df_plot, run_dir)
                        plot_files += write_convergence_overview_plot(df_plot, idx_df, run_dir)
                        plot_files += write_app_type_plots(df_plot, idx_df, run_dir)
                        plot_files += write_data_samples_plot(df_plot, idx_df, run_dir)
                if idx_df is not None and not idx_df.empty:
                    idx_plot = idx_df.dropna(subset=["round_id"])
                    if not idx_plot.empty:
                        plot_files += write_network_qos_plots(idx_plot, run_dir)
                        plot_files += write_resource_plots(idx_plot, df, run_dir)
                        plot_files += write_device_summary_plot(idx_plot, run_dir)
                        plot_files += write_epoch_distribution_plots(idx_plot, run_dir)
                if df is not None and not df.empty:
                    df_plot2 = df.dropna(subset=["round_id"])
                    if not df_plot2.empty:
                        plot_files += write_latency_vs_satisfaction_plot(df_plot2, idx_df, rep, run_dir)
                plot_files += write_upload_timing_plots(rep, run_dir)
                plot_files += write_edge_aggregation_plots(rep, run_dir)
                plot_files += write_central_timing_plots(rep, run_dir)

        # CTrace integration for this trace folder
        ctrace_section = ""
        trace_results: List[Dict[str, Any]] = []
        if trace_path.exists():
            trace_results = analyze_traces(trace_path)
            ctrace_section = _build_ctrace_markdown(trace_results, trace_path)

        # Write report
        md_text = build_markdown(rep, plot_filenames=plot_files if plot_files else None)
        if ctrace_section:
            md_text += "\n\n" + ctrace_section
        md_path = run_dir / "trial_analysis.md"
        md_path.write_text(md_text, encoding="utf-8")

        # JSON summary
        if args.json_summary:
            summary = {
                "trace_folder": trace_path.name,
                "central_log": log_path.name,
                "trace_ts": trace_ts.isoformat(),
                "central_log_ts": log_ts.isoformat(),
                "window_start": rep.window_start.isoformat() if rep.window_start else None,
                "window_end": rep.window_end.isoformat() if rep.window_end else None,
                "counts": {
                    "telemetry_received": len(rep.telemetry_rows),
                    "index_telemetry": len(rep.index_telemetry),
                    "upload_events": len(rep.upload_events),
                    "edge_agg_meta": len(rep.edge_agg_meta),
                    "weights_received": len(rep.weights_received_rows),
                    "manifests": len(rep.manifests),
                },
                "n_rounds": n_rounds,
                "plots": plot_files,
                "ctrace_analysis": trace_results if trace_results else None,
            }
            js_path = run_dir / "trial_summary.json"
            js_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        print(f"  OK: {run_dir.name} ({n_rounds} rounds, {len(plot_files)} plots)", file=sys.stderr)
        success_count += 1

    # Summary
    print(f"\n{'=' * 72}", file=sys.stderr)
    print(f"Done: {success_count} analyzed, {skip_count} skipped (min-rounds={args.min_rounds})", file=sys.stderr)
    print(f"Output: {base_out}", file=sys.stderr)

    # Write mapping file
    map_path = base_out / "trace_central_mapping.json"
    map_data = []
    for trace_path, log_path, trace_ts, log_ts in mapping:
        map_data.append({
            "trace_folder": trace_path.name,
            "central_log": log_path.name,
            "trace_ts": trace_ts.strftime("%Y-%m-%d %H:%M:%S"),
            "central_log_ts": log_ts.strftime("%Y-%m-%d %H:%M:%S"),
            "delta_seconds": (trace_ts - log_ts).total_seconds(),
        })
    map_path.write_text(json.dumps(map_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Mapping: {map_path}", file=sys.stderr)

    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze latest HFL trial logs (comprehensive)")
    ap.add_argument("--central", type=str, default=None)
    ap.add_argument("--out-dir", type=str, default="analysis_output")
    ap.add_argument("--manifest-glob", type=str, default="received_files/terminal_updates/**/manifest_*.json")
    ap.add_argument("--json-summary", action="store_true")
    ap.add_argument("--no-plots", action="store_true")
    ap.add_argument("--min-rounds", type=int, default=5, help="Minimum rounds required to process a trial (default: 5)")
    ap.add_argument("--traces", type=str, default=None,
                    help="Path to CTrace directory (auto-detect latest if 'auto')")
    ap.add_argument("--group", nargs="+", metavar="CENTRAL_LOG",
                    help="Compare multiple trials: pass 2+ central log paths to generate a group comparison report")
    ap.add_argument("--trace-map", type=str, default=None,
                    help="Path to trace folder parent (e.g. before_memory_policy/). "
                         "Auto-maps each trace folder to a central log and runs single-trial analysis per trace.")
    args = ap.parse_args()

    # ── Group comparison mode ──
    if args.group:
        return _run_group_comparison(args.group, args.out_dir, args.no_plots, args.min_rounds)

    # ── Trace-map mode: auto-map trace folders → central logs, run each ──
    if args.trace_map:
        return _run_trace_map(args)

    logs_dir = ROOT / "logs" / "time_records"
    central_path = Path(args.central) if args.central else find_latest_central(logs_dir)
    if not central_path or not central_path.is_file():
        print("No central log found.", file=sys.stderr)
        return 2
    if not central_path.is_absolute():
        central_path = (ROOT / central_path).resolve()

    rep = TrialReport(central_path=central_path)

    # 1. Parse central server log
    print("Ingesting central server log...", file=sys.stderr)
    parse_central(central_path, rep)
    window = (rep.window_start, rep.window_end)

    # 2. Parse edge JSONL logs
    print("Ingesting edge server logs...", file=sys.stderr)
    jsonl_files = collect_round_and_edge_logs(window)
    total_lines = 0
    for p in jsonl_files:
        total_lines += ingest_jsonl_file(p, rep, window)

    # 3. Load manifests
    rep.manifests = load_manifests_for_window(window, args.manifest_glob)

    # 4. Load post-switch telemetry
    rep.post_switch_telemetry = load_post_switch_telemetry(window)

    # 5. Load FULL index.jsonl (rich data)
    print("Ingesting index.jsonl (rich telemetry)...", file=sys.stderr)
    rep.index_telemetry = load_full_index_telemetry(window)

    # 6. Enrich telemetry_rows from index data
    _enrich_telemetry_from_index(rep)

    # 7. Load upload timing events
    print("Ingesting upload event logs...", file=sys.stderr)
    rep.upload_events = load_upload_events(window)

    # 8. Load edge aggregation metadata
    print("Ingesting edge aggregation metadata...", file=sys.stderr)
    rep.edge_agg_meta = load_edge_agg_meta(window)

    # Output directory
    base_out = (ROOT / args.out_dir).resolve()
    base_out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = base_out / f"trial_run_{stamp}_{central_path.stem}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Build DataFrames
    df = _telemetry_dataframe(rep)
    idx_df = _index_telemetry_dataframe(rep)

    n_rounds = 0
    if df is not None and "round_id" in df.columns:
        n_rounds = df["round_id"].nunique()
    
    if n_rounds < args.min_rounds:
        print(f"WARN: Aborting analysis. Only {n_rounds} rounds completed (requires --min-rounds {args.min_rounds}).", file=sys.stderr)
        return 2

    # Generate plots
    plot_files: List[str] = []
    if not args.no_plots:
        try:
            _setup_matplotlib()
        except ImportError:
            print("WARN: matplotlib not available, skipping plots.", file=sys.stderr)
        else:
            if df is not None and not df.empty:
                df_plot = df.dropna(subset=["round_id"])
                if not df_plot.empty:
                    plot_files += write_accuracy_loss_plots(df_plot, run_dir)
                    plot_files += write_training_time_plots(df_plot, run_dir)
                    plot_files += write_satisfaction_plots(df_plot, rep, run_dir)
                    plot_files += write_weight_norm_latency_plots(df_plot, run_dir)
                    plot_files += write_ap_distribution_plots(df_plot, run_dir)
                    plot_files += write_convergence_overview_plot(df_plot, idx_df, run_dir)
                    plot_files += write_app_type_plots(df_plot, idx_df, run_dir)
                    plot_files += write_data_samples_plot(df_plot, idx_df, run_dir)

            if idx_df is not None and not idx_df.empty:
                idx_plot = idx_df.dropna(subset=["round_id"])
                if not idx_plot.empty:
                    plot_files += write_network_qos_plots(idx_plot, run_dir)
                    plot_files += write_resource_plots(idx_plot, df, run_dir)
                    plot_files += write_device_summary_plot(idx_plot, run_dir)
                    plot_files += write_epoch_distribution_plots(idx_plot, run_dir)

            # Latency vs Satisfaction scatter (needs both df and idx_df)
            if df is not None and not df.empty:
                df_plot2 = df.dropna(subset=["round_id"])
                if not df_plot2.empty:
                    plot_files += write_latency_vs_satisfaction_plot(df_plot2, idx_df, rep, run_dir)

            plot_files += write_upload_timing_plots(rep, run_dir)
            plot_files += write_edge_aggregation_plots(rep, run_dir)
            plot_files += write_central_timing_plots(rep, run_dir)

    # CTrace analysis integration
    ctrace_section = ""
    trace_results: List[Dict[str, Any]] = []
    trace_dir_arg = getattr(args, "traces", None)
    if trace_dir_arg:
        if trace_dir_arg == "auto":
            trace_dir = _find_latest_trace_dir()
        else:
            trace_dir = Path(trace_dir_arg)
        if trace_dir and trace_dir.exists():
            print(f"Analyzing CTrace files in {trace_dir}...", file=sys.stderr)
            trace_results = analyze_traces(trace_dir)
            ctrace_section = _build_ctrace_markdown(trace_results, trace_dir)
        else:
            print(f"WARN: Trace directory not found: {trace_dir_arg}", file=sys.stderr)

    # Write report
    md_path = run_dir / "trial_analysis.md"
    md_text = build_markdown(rep, plot_filenames=plot_files if plot_files else None)
    if ctrace_section:
        md_text += "\n\n" + ctrace_section
    md_path.write_text(md_text, encoding="utf-8")

    print(f"\nRun bundle: {run_dir}")
    print(f"Report:     {md_path}")
    print(f"  telemetry={len(rep.telemetry_rows)}, index={len(rep.index_telemetry)}, "
          f"uploads={len(rep.upload_events)}, edge_agg={len(rep.edge_agg_meta)}, "
          f"weights={len(rep.weights_received_rows)}, manifests={len(rep.manifests)}")
    if plot_files:
        print(f"  {len(plot_files)} plots generated:")
        for pf in plot_files:
            print(f"    {pf}")

    if args.json_summary:
        # Compute per-round accuracy std across terminals (intra-trial variance)
        accuracy_std_per_round = {}
        if df is not None and "accuracy" in df.columns and "round_id" in df.columns:
            for rid, grp in df.groupby("round_id"):
                vals = grp["accuracy"].dropna().tolist()
                if len(vals) >= 2:
                    mean_v = sum(vals) / len(vals)
                    std_v = (sum((x - mean_v) ** 2 for x in vals) / (len(vals) - 1)) ** 0.5
                    accuracy_std_per_round[str(rid)] = {"mean": mean_v, "std": std_v, "n": len(vals)}

        summary = {
            "run_directory": str(run_dir.relative_to(ROOT)),
            "central_log": str(central_path.relative_to(ROOT)),
            "window_start": rep.window_start.isoformat() if rep.window_start else None,
            "window_end": rep.window_end.isoformat() if rep.window_end else None,
            "counts": {
                "telemetry_received": len(rep.telemetry_rows),
                "index_telemetry": len(rep.index_telemetry),
                "upload_events": len(rep.upload_events),
                "edge_agg_meta": len(rep.edge_agg_meta),
                "weights_received": len(rep.weights_received_rows),
                "manifests": len(rep.manifests),
                "central_timing": len(rep.central_timing),
            },
            "accuracy_std_per_round": accuracy_std_per_round,
            "plots": plot_files,
            "ctrace_analysis": trace_results if trace_results else None,
        }
        js_path = run_dir / "trial_summary.json"
        js_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  JSON: {js_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
