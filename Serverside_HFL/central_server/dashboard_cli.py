#!/usr/bin/env python3
"""Simple CLI dashboard for HFL Central.

This is a minimal, port-free alternative to the Streamlit dashboard.
It prints a short summary of the central training DB and summaries of
manifest JSONs found under `received_files/terminal_updates`.

Run:
  python central_server/dashboard_cli.py

Optional args:
  --db PATH            Path to training_data.db (default: central_server/training_data.db)
  --manifests DIR      Root directory containing terminal_updates (default: received_files/terminal_updates)
  --top N              Show top N sha hashes (default 20)
"""
import argparse
import glob
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path
from datetime import datetime


def load_db_summary(db_path: Path):
    summary = {}
    if not db_path.exists():
        summary['exists'] = False
        return summary
    summary['exists'] = True
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("SELECT COUNT(*) FROM training_data")
        summary['rows'] = cur.fetchone()[0]
        # try to show columns (best-effort)
        cur = conn.execute("PRAGMA table_info(training_data)")
        cols = [r[1] for r in cur.fetchall()]
        summary['columns'] = cols
    except Exception:
        summary['rows'] = 0
        summary['columns'] = []
    finally:
        conn.close()
    return summary


def scan_manifests(manifests_root: Path):
    pattern = str(manifests_root.joinpath('**', 'manifest_*.json'))
    files = glob.glob(pattern, recursive=True)
    rows = []
    for p in sorted(files, reverse=True):
        try:
            with open(p, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
        except Exception:
            continue
        sha = data.get('sha256') or data.get('sha')
        run_id = data.get('run_id') or data.get('run')
        evt = data.get('event_timestamp') or data.get('started_at') or data.get('timestamp')
        rows.append((p, sha, run_id, evt))
    return rows


def human_time(ts):
    if not ts:
        return ''
    try:
        return datetime.fromisoformat(ts).isoformat()
    except Exception:
        return str(ts)


def main():
    repo_root = Path(__file__).resolve().parent.parent
    default_db = repo_root.joinpath('central_server', 'training_data.db')
    default_manifests = repo_root.joinpath('received_files', 'terminal_updates')

    p = argparse.ArgumentParser()
    p.add_argument('--db', default=str(default_db))
    p.add_argument('--manifests', default=str(default_manifests))
    p.add_argument('--top', default=20, type=int)
    args = p.parse_args()

    db_path = Path(args.db)
    manifests_root = Path(args.manifests)

    print('HFL Central — Simple CLI Dashboard')
    print('Repository root:', repo_root)
    print()

    db_summary = load_db_summary(db_path)
    if not db_summary.get('exists'):
        print(f'No training DB found at {db_path} — training table may be empty or DB missing')
    else:
        print(f'Training DB: {db_path}')
        print(f' - rows: {db_summary.get("rows")}')
        cols = db_summary.get('columns') or []
        print(f' - columns: {cols[:10]}{"..." if len(cols)>10 else ""}')

    print()
    print(f'Scanning manifests under: {manifests_root} (pattern manifest_*.json)')
    rows = scan_manifests(manifests_root)
    print(f' - manifests found: {len(rows)}')
    if rows:
        # top sha
        shas = [r[1] for r in rows if r[1]]
        cnt = Counter(shas)
        print('\nTop SHA frequency:')
        for i, (sha, c) in enumerate(cnt.most_common(args.top), start=1):
            print(f' {i:2d}. {sha}  ({c})')

        print('\nRecent manifests (up to 20):')
        for p, sha, run, evt in rows[:20]:
            print(f' - {Path(p).name} | sha={sha} | run_id={run} | ts={human_time(evt)}')

    print('\nDone')


if __name__ == '__main__':
    main()
