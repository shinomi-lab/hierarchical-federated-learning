#!/usr/bin/env python3
import re
from pathlib import Path
from datetime import datetime
import shutil

ROOT = Path(__file__).resolve().parent.parent
TIME_RECORDS = ROOT / 'logs' / 'time_records'
BUNDLES = ROOT / 'logs' / 'bundles'
BUNDLES.mkdir(parents=True, exist_ok=True)

CENTRAL_RE = re.compile(r'central_server_(\d{8}_\d{6})')
EDGE_RE = re.compile(r'edge_server(?:_[^_]*)?_(\d{8}_\d{6})')
TS_FMT = '%Y%m%d_%H%M%S'
MAX_DELTA_SEC = 300  # 5 minutes

def ts_from_name(name, pattern):
    m = pattern.search(name)
    if not m:
        return None
    return datetime.strptime(m.group(1), TS_FMT)

def find_pairs():
    central_files = list(TIME_RECORDS.glob('central_server_*.log'))
    edge_files = list(TIME_RECORDS.glob('edge_server*_*_*.log')) + list(TIME_RECORDS.glob('edge_server_*.log'))
    # normalize unique
    edge_files = list({p: None for p in edge_files}.keys())

    pairs = []
    used_edges = set()
    for cf in central_files:
        cts = ts_from_name(cf.name, CENTRAL_RE)
        if not cts:
            continue
        # find all edge files within window (allow multiple edges per central)
        matches = []
        for ef in edge_files:
            if ef in used_edges:
                continue
            ets = ts_from_name(ef.name, EDGE_RE)
            if not ets:
                continue
            delta = abs((ets - cts).total_seconds())
            if delta <= MAX_DELTA_SEC:
                matches.append((delta, ef))
        # sort by proximity and include all matches
        matches.sort(key=lambda x: x[0])
        matched_edges = [ef for _d, ef in matches]
        for ef in matched_edges:
            used_edges.add(ef)
        pairs.append((cf, matched_edges))
    return pairs


def bundle_pair(central, edges, dry=False):
    cts = CENTRAL_RE.search(central.name).group(1)
    bundle_name = f"{cts}_bundle"
    dest = BUNDLES / bundle_name
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    def _copy(src):
        dst = dest / src.name
        if not dst.exists():
            if not dry:
                shutil.copy2(src, dst)
            copied.append(src.name)
    _copy(central)
    for edge in edges or []:
        _copy(edge)
    return dest, copied

if __name__ == '__main__':
    pairs = find_pairs()
    created = 0
    total_copied = 0
    for central, edges in pairs:
        dest, copied = bundle_pair(central, edges, dry=False)
        if copied:
            created += 1
            total_copied += len(copied)
            print(f"Bundled: {dest} <- {', '.join(copied)}")
    print(f"Done. Bundles created: {created}, files copied: {total_copied}")
