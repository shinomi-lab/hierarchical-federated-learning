import json
import csv
import glob
import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional
import argparse

ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = ROOT / "logs" / "analysis"
ROUND_SUMMARY = ROOT / "logs" / "rounds" / "round_summary.jsonl"
TERMINAL_UPDATES_DIR = ROOT / "received_files" / "terminal_updates"
TERMINAL_LOGS_DIR = ROOT / "received_files" / "terminal_logs"
RECEIVED_EDGES_DIR = ROOT / "received_edges"
METRICS_DB_PATH = ROOT / "central_server" / "training_metrics.db"


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_csv(path: Path, rows: List[Dict[str, Any]]):
    if not rows:
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def collect_terminal_updates(day: Optional[str] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for rdir in sorted(glob.glob(str(TERMINAL_UPDATES_DIR / "r*/"))):
        for tdir in sorted(glob.glob(str(Path(rdir) / "*/"))):
            for mpath in sorted(glob.glob(str(Path(tdir) / "manifest_*.json"))):
                m = read_json(Path(mpath))
                if not m:
                    continue
                # Optional day filter by saved_at prefix YYYYMMDD
                if day:
                    if str(m.get("saved_at", ""))[:8] != day:
                        continue
                row = {
                    "kind": "terminal_update",
                    "terminal_id": m.get("terminal_id"),
                    "round": m.get("round"),
                    "model_id": m.get("model_id"),
                    "base_hash": m.get("base_hash"),
                    "n_samples": m.get("n_samples"),
                    "sha256": m.get("sha256"),
                    "dtype": m.get("dtype"),
                    "payload_kind": m.get("payload_kind"),
                    "filename": m.get("filename"),
                    "size": m.get("size"),
                    "saved_at": m.get("saved_at"),
                    "reqId": m.get("reqId"),
                    "app_type": m.get("app_type"),
                    "app_index": m.get("app_index"),
                    "satisfaction_before": m.get("satisfaction_before"),
                    "satisfaction_after": m.get("satisfaction_after"),
                }
                # client_meta may be large; include as string
                if "client_meta" in m:
                    row["client_meta"] = m.get("client_meta")
                rows.append(row)
    return rows


def collect_terminal_logs_index(day: Optional[str] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for tdir in sorted(glob.glob(str(TERMINAL_LOGS_DIR / "*/"))):
        ipath = Path(tdir) / "index.jsonl"
        entries = read_jsonl(ipath)
        for e in entries:
            if day and e.get("date") != day:
                continue
            e["kind"] = "terminal_log"
            rows.append(e)
    return rows


def collect_edge_manifests(day: Optional[str] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for rdir in sorted(glob.glob(str(RECEIVED_EDGES_DIR / "r*/"))):
        for edir in sorted(glob.glob(str(Path(rdir) / "*/"))):
            for mpath in sorted(glob.glob(str(Path(edir) / "manifest_*.json"))):
                m = read_json(Path(mpath))
                if not m:
                    continue
                if day and str(m.get("saved_at", ""))[:8] != day:
                    continue
                rows.append({
                    "kind": "edge_manifest",
                    "edge_id": m.get("edge_id"),
                    "round": m.get("round"),
                    "num_clients": m.get("num_clients"),
                    "sum_n_samples": m.get("sum_n_samples"),
                    "sha256": m.get("sha256"),
                    "saved_at": m.get("saved_at"),
                    "path": m.get("path"),
                })
    return rows


def collect_metrics_db(day: Optional[str] = None) -> List[Dict[str, Any]]:
    """training_metrics.db の metrics テーブルを読み取る。"""
    if not METRICS_DB_PATH.exists():
        return []
    rows = []
    try:
        with sqlite3.connect(str(METRICS_DB_PATH), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM metrics ORDER BY id")
            for row in cur.fetchall():
                r = dict(row)
                r["kind"] = "metrics_db"
                if day and str(r.get("event_timestamp", ""))[:8].replace("-", "") != day:
                    continue
                rows.append(r)
    except Exception:
        pass
    return rows


def collect_round_summary(day: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = read_jsonl(ROUND_SUMMARY)
    if day:
        rows = [r for r in rows if str(r.get("time", ""))[:10].replace("-", "") == day]
    for r in rows:
        r["kind"] = "round_summary"
    return rows


def aggregate_by_round(terminal_updates: List[Dict[str, Any]], edge_manifests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    # Terminal updates
    for t in terminal_updates:
        rnd = int(t.get("round") or -1)
        if rnd < 0:
            continue
        agg = out.setdefault(rnd, {"round": rnd, "terminals": 0, "sum_n_samples": 0})
        agg["terminals"] += 1
        try:
            agg["sum_n_samples"] += int(t.get("n_samples") or 0)
        except Exception:
            pass
    # Edge manifests
    for e in edge_manifests:
        rnd = int(e.get("round") or -1)
        if rnd < 0:
            continue
        agg = out.setdefault(rnd, {"round": rnd, "terminals": 0, "sum_n_samples": 0})
        try:
            agg["edges_sum_n_samples"] = agg.get("edges_sum_n_samples", 0) + int(e.get("sum_n_samples") or 0)
        except Exception:
            pass
        agg["edges"] = agg.get("edges", 0) + 1
    return sorted(out.values(), key=lambda x: x["round"])


def main():
    parser = argparse.ArgumentParser(description="Aggregate training-related data from central and edge")
    parser.add_argument("--day", help="YYYYMMDD to restrict collection", default=None)
    parser.add_argument("--prefix", help="output file prefix", default="aggregate")
    args = parser.parse_args()

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

    t_updates = collect_terminal_updates(args.day)
    t_logs = collect_terminal_logs_index(args.day)
    e_manifests = collect_edge_manifests(args.day)
    r_summary = collect_round_summary(args.day)
    r_agg = aggregate_by_round(t_updates, e_manifests)
    metrics = collect_metrics_db(args.day)

    def dump(name: str, rows: List[Dict[str, Any]]):
        json_path = ANALYSIS_DIR / f"{args.prefix}_{name}_{args.day or 'all'}.json"
        csv_path = ANALYSIS_DIR / f"{args.prefix}_{name}_{args.day or 'all'}.csv"
        with open(json_path, "w", encoding="utf-8") as jf:
            json.dump(rows, jf, ensure_ascii=False, indent=2)
        write_csv(csv_path, rows)

    dump("terminal_updates", t_updates)
    dump("terminal_logs", t_logs)
    dump("edge_manifests", e_manifests)
    dump("round_summary", r_summary)
    dump("round_aggregate", r_agg)
    dump("metrics", metrics)

    print("Aggregation complete.")
    print(f" - terminal_updates: {len(t_updates)} rows")
    print(f" - terminal_logs: {len(t_logs)} rows")
    print(f" - edge_manifests: {len(e_manifests)} rows")
    print(f" - round_summary: {len(r_summary)} rows")
    print(f" - round_aggregate: {len(r_agg)} rows")
    print(f" - metrics (DB): {len(metrics)} rows")


if __name__ == "__main__":
    main()
