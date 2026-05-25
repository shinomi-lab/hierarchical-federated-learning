#!/usr/bin/env python3
"""Organize log files by timestamp groups.

Produces folders under `logs/organized/<YYYYMMDD_HHMM>/` and copies or moves
log files whose filenames contain timestamps like `YYYYMMDD_HHMMSS`.

Usage:
  python scripts/organize_logs.py --target logs/organized --group-by minute --move
  python scripts/organize_logs.py --dry-run
"""
from pathlib import Path
import re
import argparse
import shutil
from datetime import datetime

TS_RE = re.compile(r"(\d{8}_\d{6})")
SEARCH_DIRS = [Path('logs/time_records'), Path('logs/rounds')]


def find_timestamp_from_name(p: Path):
    m = TS_RE.search(p.name)
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y%m%d_%H%M%S')
        except Exception:
            return None
    return None


def group_key(dt: datetime, by: str):
    if by == 'minute':
        return dt.strftime('%Y%m%d_%H%M')
    if by == 'hour':
        return dt.strftime('%Y%m%d_%H')
    return dt.strftime('%Y%m%d')


def collect_logs():
    files = []
    for d in SEARCH_DIRS:
        if not d.exists():
            continue
        for p in d.rglob('*.log'):
            files.append(p)
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--group-by', choices=['minute', 'hour', 'day'], default='minute')
    ap.add_argument('--target', default='logs/organized')
    ap.add_argument('--move', action='store_true', help='Move files instead of copying')
    ap.add_argument('--dry-run', action='store_true', help='Show actions without performing them')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    target = Path(args.target)
    files = collect_logs()
    if args.verbose:
        print(f'Found {len(files)} .log files to consider')

    actions = []
    for f in files:
        dt = find_timestamp_from_name(f)
        if dt is None:
            # fallback to file mtime
            try:
                mtime = f.stat().st_mtime
                dt = datetime.fromtimestamp(mtime)
            except Exception:
                continue
        key = group_key(dt, args.group_by)
        dest_dir = target / key
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f.name
        actions.append((f, dest))

    if args.dry_run:
        print('Dry run: the following actions would be performed:')
        for src, dst in actions:
            print(f'  {src} -> {dst}')
        return

    for src, dst in actions:
        if args.move:
            try:
                shutil.move(str(src), str(dst))
                if args.verbose:
                    print(f'MOVED {src} -> {dst}')
            except Exception as e:
                print(f'ERROR moving {src} -> {dst}: {e}')
        else:
            try:
                shutil.copy2(str(src), str(dst))
                if args.verbose:
                    print(f'COPIED {src} -> {dst}')
            except Exception as e:
                print(f'ERROR copying {src} -> {dst}: {e}')

    # Build index of group directories under target and write descending-order index
    groups = []
    if target.exists():
        for p in sorted(target.iterdir()):
            if p.is_dir():
                groups.append(p.name)

    # sort descending (newest first). group keys are like YYYYMMDD_HHMM
    groups_sorted = sorted(groups, reverse=True)
    index_path = target / 'INDEX.json'
    try:
        import json
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(index_path, 'w', encoding='utf-8') as f:
            json.dump({'groups_desc': groups_sorted}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'Failed to write index: {e}')

    # write LATEST pointer
    latest_path = target / 'LATEST'
    try:
        if groups_sorted:
            with open(latest_path, 'w', encoding='utf-8') as f:
                f.write(groups_sorted[0])
        else:
            if latest_path.exists():
                latest_path.unlink()
    except Exception as e:
        print(f'Failed to write LATEST file: {e}')

    print(f'Done. Organized {len(actions)} files under {target} (move={args.move}).')


if __name__ == '__main__':
    main()
