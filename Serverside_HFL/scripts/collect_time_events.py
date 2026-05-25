"""Collect recent time_records events matching patterns and print them.

Usage: python scripts/collect_time_events.py [--lines N]
"""
import sys
from pathlib import Path
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--lines', type=int, default=200)
args = parser.parse_args()

root = Path(__file__).resolve().parents[1] / 'logs' / 'time_records'
if not root.exists():
    print('No logs/time_records found at', root)
    sys.exit(0)

patterns = ['upload_streamed', 'request_e2e', 'duplicate_ignored', 'received_persisted']

files = sorted(list(root.glob('central_server_*.log')), key=lambda p: p.stat().st_mtime, reverse=True)
out_lines = []
for f in files:
    txt = f.read_text(encoding='utf-8', errors='ignore')
    for line in txt.splitlines():
        for pat in patterns:
            if pat in line:
                out_lines.append((f.name, line))

for name, line in out_lines[:args.lines]:
    print(f"[{name}] {line}")
