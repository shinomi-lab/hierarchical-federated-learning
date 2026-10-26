#!/usr/bin/env python3
"""端末の metrics_ledger.jsonl と中央 metrics DB を data_id で照合する。

回収: adb -s <serial> exec-out run-as com.example.hfl_experiment cat files/metrics_ledger.jsonl > ledger_<terminal>.jsonl
実行: Serverside_HFL/.venv/bin/python3 scripts/reconcile_metrics.py --db received_edges/training_metrics.db ledger_*.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

FIELDS = ["accuracy", "loss", "satisfaction_before", "satisfaction_after", "satisfaction_cycle",
          "tp_measured_mbps", "rtt_measured_ms", "app_type", "virtual_router_id", "cycle_number", "run_id"]


def _load_ledger(paths):
    rows = {}
    for p in paths:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                jo = json.loads(line)
            except json.JSONDecodeError:
                continue
            if jo.get("kind") == "experiment_event":
                continue  # モード切替などの実験イベントはメトリクスの照合対象外
            if jo.get("data_id"):
                rows[jo["data_id"]] = jo  # 後の行が有効
    return rows


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-9)
    return a == b


def reconcile(ledger_paths, db_path) -> dict:
    ledger = _load_ledger(ledger_paths)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        ids = list(ledger)
        db = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = f"SELECT * FROM metrics WHERE data_id IN ({','.join('?' * len(chunk))})"
            db.update({r["data_id"]: dict(r) for r in conn.execute(q, chunk)})
    terminals: dict = {}
    for did, row in ledger.items():
        t = terminals.setdefault(row.get("terminal_id", "?"), {"ledger": 0, "in_db": 0, "missing": [], "mismatched": []})
        t["ledger"] += 1
        got = db.get(did)
        if got is None:
            t["missing"].append(did)
            continue
        t["in_db"] += 1
        for f in FIELDS:
            if row.get(f) is not None and f in got and not _same(row[f], got[f]):
                t["mismatched"].append({"data_id": did, "field": f, "ledger": row[f], "db": got[f]})
    return {"terminals": terminals, "missing_total": sum(len(t["missing"]) for t in terminals.values())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("ledgers", nargs="+")
    a = ap.parse_args(argv)
    r = reconcile(a.ledgers, a.db)
    for tid, t in sorted(r["terminals"].items()):
        print(f"{tid}: ledger={t['ledger']} in_db={t['in_db']} missing={len(t['missing'])} mismatched={len(t['mismatched'])}")
        for did in t["missing"]:
            print(f"  missing {did}")
        for m in t["mismatched"]:
            print(f"  mismatch {m['data_id']} {m['field']}: ledger={m['ledger']} db={m['db']}")
    print(f"missing_total={r['missing_total']}")
    return 0 if r["missing_total"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
