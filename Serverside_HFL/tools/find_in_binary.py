#!/usr/bin/env python3
import sys
from pathlib import Path

if len(sys.argv) < 3:
    print("Usage: find_in_binary.py <file> <needle>")
    raise SystemExit(2)

p = Path(sys.argv[1])
needle = sys.argv[2].encode('utf-8')
try:
    data = p.read_bytes()
except Exception as e:
    print('ERROR', e)
    raise SystemExit(1)

i = data.find(needle)
if i == -1:
    print('NOT_FOUND')
    raise SystemExit(0)

start = max(0, i-200)
end = min(len(data), i+len(needle)+200)
ctx = data[start:end]
print('FOUND')
try:
    print(ctx.decode('utf-8', errors='replace'))
except Exception:
    print(ctx)
