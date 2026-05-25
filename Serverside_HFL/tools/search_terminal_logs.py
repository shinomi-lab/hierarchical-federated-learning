#!/usr/bin/env python3
import sys
from pathlib import Path

if len(sys.argv) < 3:
    print("Usage: search_terminal_logs.py <dir> <needle>")
    raise SystemExit(2)

dirp = Path(sys.argv[1])
needle = sys.argv[2].encode('utf-8')
found_any = False
for p in dirp.rglob('*'):
    if not p.is_file():
        continue
    try:
        data = p.read_bytes()
    except Exception:
        continue
    if needle in data:
        print(f'FOUND IN: {p}')
        i = data.find(needle)
        start = max(0, i-200)
        end = min(len(data), i+len(needle)+200)
        ctx = data[start:end]
        try:
            print(ctx.decode('utf-8', errors='replace'))
        except Exception:
            print(ctx)
        found_any = True
if not found_any:
    print('NOT_FOUND')
