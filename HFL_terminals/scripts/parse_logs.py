#!/usr/bin/env python3
"""
parse_logs.py

Usage:
  python scripts/parse_logs.py --input DIR --output merged.csv

This script scans DIR for:
 - *_log.txt (server-style lines like: 2025-12-08 14:20:11,031 - INFO - Tag - message)
 - training_log.csv (CSV produced by TrainingSimpleLogger)
 - training_events.log (CSV with header timestamp,event,round,extra)

It outputs a unified CSV with normalized ISO timestamps and columns to help analysis.

"""
import re
import csv
import argparse
import os
import datetime
from pathlib import Path

SERVER_LINE_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - (?P<level>[^ ]+) - (?P<tag>[^ ]+) - (?P<msg>.*)$")


def parse_server_log(fp: Path):
    rows = []
    with fp.open('r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip('\n')
            m = SERVER_LINE_RE.match(line)
            if m:
                ts = m.group('ts')
                # convert ts like 2025-12-08 14:20:11,031 to ISO
                try:
                    dt = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S,%f")
                    iso = dt.isoformat(timespec='milliseconds')
                except Exception:
                    iso = ts
                rows.append({
                    'ts_iso': iso,
                    'source': fp.name,
                    'type': 'server_log',
                    'level': m.group('level'),
                    'tag': m.group('tag'),
                    'event': None,
                    'round': None,
                    'epoch': None,
                    'progress': None,
                    'val_loss': None,
                    'val_acc': None,
                    'msg': m.group('msg')
                })
            else:
                # fallback: put entire line into msg
                rows.append({
                    'ts_iso': None,
                    'source': fp.name,
                    'type': 'server_log',
                    'level': None,
                    'tag': None,
                    'event': None,
                    'round': None,
                    'epoch': None,
                    'progress': None,
                    'val_loss': None,
                    'val_acc': None,
                    'msg': line
                })
    return rows


def parse_training_log(fp: Path):
    rows = []
    with fp.open('r', encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        for r in reader:
            ts_ms = r.get('timestamp_ms') or r.get('timestamp')
            iso = None
            try:
                if ts_ms and ts_ms.isdigit():
                    dt = datetime.datetime.fromtimestamp(int(ts_ms)/1000.0)
                    iso = dt.isoformat(timespec='milliseconds')
            except Exception:
                iso = ts_ms
            rows.append({
                'ts_iso': iso,
                'source': fp.name,
                'type': r.get('type') or 'training_log',
                'level': None,
                'tag': None,
                'event': None,
                'round': None,
                'epoch': r.get('epoch') or None,
                'progress': r.get('progress') or None,
                'val_loss': r.get('val_loss') or None,
                'val_acc': r.get('val_acc') or None,
                'msg': (r.get('msg') or '').replace('\n',' ')
            })
    return rows


def parse_events_log(fp: Path):
    rows = []
    with fp.open('r', encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        for r in reader:
            ts = r.get('timestamp') or r.get('timestamp_ms')
            iso = None
            try:
                if ts:
                    # try parse common formats
                    try:
                        dt = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
                        iso = dt.isoformat(timespec='milliseconds')
                    except Exception:
                        if ts.isdigit():
                            dt = datetime.datetime.fromtimestamp(int(ts)/1000.0)
                            iso = dt.isoformat(timespec='milliseconds')
                        else:
                            iso = ts
            except Exception:
                iso = ts
            rows.append({
                'ts_iso': iso,
                'source': fp.name,
                'type': 'event',
                'level': None,
                'tag': None,
                'event': r.get('event'),
                'round': r.get('round'),
                'epoch': None,
                'progress': None,
                'val_loss': None,
                'val_acc': None,
                'msg': r.get('extra') or ''
            })
    return rows


def discover_and_parse(indir: Path):
    all_rows = []
    # server-style logs: *_log.txt
    for p in sorted(indir.glob('*.txt')):
        try:
            all_rows.extend(parse_server_log(p))
        except Exception as e:
            print(f"Failed parse server log {p}: {e}")
    # training_log.csv
    tlog = indir / 'training_log.csv'
    if tlog.exists():
        try:
            all_rows.extend(parse_training_log(tlog))
        except Exception as e:
            print(f"Failed parse training_log.csv: {e}")
    # training_events.log
    ev = indir / 'training_events.log'
    if ev.exists():
        try:
            all_rows.extend(parse_events_log(ev))
        except Exception as e:
            print(f"Failed parse training_events.log: {e}")
    return all_rows


def write_merged(rows, out_fp: Path):
    fieldnames = ['ts_iso','source','type','level','tag','event','round','epoch','progress','val_loss','val_acc','msg']
    with out_fp.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ('' if r.get(k) is None else r.get(k)) for k in fieldnames})


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', '-i', help='Input folder containing logs (filesDir/log or pulled shared_logs)', required=True)
    p.add_argument('--output', '-o', help='Output CSV merged file', default='merged_logs.csv')
    args = p.parse_args()
    indir = Path(args.input)
    if not indir.exists() or not indir.is_dir():
        print(f'Input {indir} does not exist or is not a directory')
        return
    rows = discover_and_parse(indir)
    # sort by timestamp when available
    def sort_key(r):
        ts = r.get('ts_iso')
        try:
            if ts:
                return datetime.datetime.fromisoformat(ts)
        except Exception:
            return datetime.datetime.min
        return datetime.datetime.min
    rows_sorted = sorted(rows, key=sort_key)
    out_fp = Path(args.output)
    write_merged(rows_sorted, out_fp)
    print(f'Wrote {len(rows_sorted)} rows to {out_fp.resolve()}')

if __name__ == '__main__':
    main()

