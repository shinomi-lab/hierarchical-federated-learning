#!/usr/bin/env python3
from pathlib import Path
import json
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--target', default='logs/organized')
parser.add_argument('--groups', type=int, default=10, help='number of groups to show')
parser.add_argument('--files', type=int, default=20, help='number of files per group to show')
args = parser.parse_args()

target = Path(args.target)
if not target.exists():
    print(f'{target} not found')
    raise SystemExit(1)

index_file = target / 'INDEX.json'
if index_file.exists():
    try:
        idx = json.loads(index_file.read_text(encoding='utf-8'))
        groups = idx.get('groups_desc', [])
    except Exception:
        groups = [p.name for p in sorted(target.iterdir(), reverse=True) if p.is_dir()]
else:
    groups = [p.name for p in sorted(target.iterdir(), reverse=True) if p.is_dir()]

count = 0
for g in groups:
    if count >= args.groups:
        break
    gpath = target / g
    if not gpath.exists():
        continue
    print(f'=== {g} ===')
    files = sorted(list(gpath.iterdir()), key=lambda p: p.stat().st_mtime, reverse=True)
    shown = 0
    for f in files:
        if shown >= args.files:
            break
        try:
            size = f.stat().st_size
            mtime = f.stat().st_mtime
            from datetime import datetime
            print(f"{f.name}\t{size} bytes\t{datetime.fromtimestamp(mtime).isoformat()}")
        except Exception as e:
            print(f"{f.name}\tERROR: {e}")
        shown += 1
    count += 1

print(f'Listed {count} groups from {target}')
