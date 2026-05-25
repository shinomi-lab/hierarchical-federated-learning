"""
Simple CLI to scan time_records log files for events of interest.

Usage examples:
  python central_server/scripts/log_scan.py --logs logs/time_records --keyword duplicate_ignored --round 25
  python central_server/scripts/log_scan.py --logs logs/time_records --edge edge-server-01 --start 2025-11-05T22:52:00 --end 2025-11-05T22:54:00

This tool is intentionally lightweight and performs substring matches on each log line.
"""
import argparse
from pathlib import Path
import re
import sys
from datetime import datetime


def parse_args():
    p = argparse.ArgumentParser(description='Scan logs for events')
    p.add_argument('--logs', default='logs/time_records', help='directory containing time_records logs')
    p.add_argument('--keyword', action='append', help='keyword to match (can be repeated)')
    p.add_argument('--round', type=str, help='round number to filter for (as substring)')
    p.add_argument('--edge', type=str, help='edge id to filter for (as substring)')
    p.add_argument('--terminal', type=str, help='terminal id to filter for (as substring)')
    p.add_argument('--start', type=str, help='start ISO datetime (inclusive)')
    p.add_argument('--end', type=str, help='end ISO datetime (inclusive)')
    p.add_argument('--max-files', type=int, default=50, help='max number of files to scan')
    return p.parse_args()


def parse_iso(s: str):
    # accept common ISO formats
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _scan_file_worker(args):
    path, filters = args
    results = []
    try:
        with path.open('r', encoding='utf-8', errors='ignore') as fh:
            for lineno, line in enumerate(fh, start=1):
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                ok = True
                # evaluate cheap filters first: keyword match (fast substring)
                if filters['keywords']:
                    if not any(k in line_stripped for k in filters['keywords']):
                        ok = False
                if ok and filters['round']:
                    if filters['round'] not in line_stripped:
                        ok = False
                if ok and filters['edge']:
                    if filters['edge'] not in line_stripped:
                        ok = False
                if ok and filters['terminal']:
                    if filters['terminal'] not in line_stripped:
                        ok = False
                # time-window filtering: only try when other filters pass
                if ok and (filters['start'] or filters['end']):
                    m = re.search(r"(\d{4}-\d{2}-\d{2}[T_ ]\d{2}:\d{2}:\d{2})", line_stripped)
                    if m:
                        try:
                            ts = datetime.fromisoformat(m.group(1).replace('_', 'T'))
                            if filters['start'] and ts < filters['start']:
                                ok = False
                            if filters['end'] and ts > filters['end']:
                                ok = False
                        except Exception:
                            pass
                if ok:
                    results.append(f"{path}:{lineno}: {line_stripped}")
    except Exception as e:
        results.append(f"Failed to scan {path}: {e}")
    return results


def scan_file(path: Path, filters, out=sys.stdout):
    # compatibility wrapper (single-threaded call)
    for line in _scan_file_worker((path, filters)):
        out.write(line + '\n')


def main():
    args = parse_args()
    ld = Path(args.logs)
    if not ld.exists():
        print(f"Logs dir not found: {ld}")
        return 2
    files = sorted([p for p in ld.iterdir() if p.is_file()])[: args.max_files]
    filt = {
        'keywords': args.keyword or [],
        'round': args.round,
        'edge': args.edge,
        'terminal': args.terminal,
        'start': parse_iso(args.start) if args.start else None,
        'end': parse_iso(args.end) if args.end else None,
    }
    # run workers in parallel over files
    from concurrent.futures import ProcessPoolExecutor, as_completed

    # prepare args list
    args_list = [(f, filt) for f in files]
    with ProcessPoolExecutor() as exe:
        futures = [exe.submit(_scan_file_worker, a) for a in args_list]
        for fut in as_completed(futures):
            try:
                res = fut.result()
                for line in res:
                    print(line)
            except Exception as e:
                print(f"Worker failure: {e}")


if __name__ == '__main__':
    exit(main())
