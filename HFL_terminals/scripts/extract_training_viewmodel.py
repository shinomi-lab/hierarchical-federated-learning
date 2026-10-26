#!/usr/bin/env python3
"""
extract_training_viewmodel.py

ワークスペース内のログファイルから "TrainingViewModel" を含むログレコードを抽出し、CSVに出力します。
- デフォルトで候補ファイルリストから存在するファイルを処理します
- 文字コードは試行的に utf-8, utf-8-sig, utf-16, shift_jis, cp932 の順で検出しますn- マルチラインログ（スタックトレース等）は、次行がログヘッダ（タイムスタンプ等）で始まらない限り継続行として結合しますn
使い方:
  python scripts/extract_training_viewmodel.py --inputs recent_device_log.txt device_live_logs.txt --output extracted_logs/training_viewmodel_extracted.csv

"""
import argparse
import csv
import os
import re
import sys

DEFAULT_CANDIDATES = [
    'recent_device_log.txt',
    'device_live_logs.txt',
    'device_logs.txt',
    'device_logs_excerpt.txt',
    'device_upload_logs.txt',
    'device_upload_excerpt.txt',
    'device_ws_log.txt',
    'device_ws_log_utf8.txt',
    'device_ws_excerpt.txt',
    'log.txt',
]

ENCODINGS_TO_TRY = ['utf-8', 'utf-8-sig', 'utf-16', 'cp932', 'shift_jis']

# Timestamp header detection (ISO, android short, yyyy-mm-dd hh:mm:ss)
TIMESTAMP_RE = re.compile(r'(\d{4}-\d{2}-\d{2}T|\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})')

# Level/tag/message detection (e.g. "12-31 23:59:59.123 I/Tag: message")
LEVEL_TAG_RE = re.compile(r"(?P<level>[VDIWEF])\/(?P<tag>[^:\s]+)\s*:\s*(?P<message>.*)")

TRAINING_KEY = 'TrainingViewModel'


def detect_encoding(path, encodings=ENCODINGS_TO_TRY):
    # read a small sample and try decode
    with open(path, 'rb') as f:
        sample = f.read(8192)
    for enc in encodings:
        try:
            sample.decode(enc)
            return enc
        except Exception:
            continue
    return None


def is_log_header(line):
    if not line:
        return False
    return bool(TIMESTAMP_RE.search(line))


def extract_timestamp(text):
    m = TIMESTAMP_RE.search(text)
    return m.group(0) if m else ''


def extract_level_tag(text):
    m = LEVEL_TAG_RE.search(text)
    if m:
        return m.group('level'), m.group('tag'), m.group('message')
    return '', '', ''


def process_file(path, writer):
    if not os.path.isfile(path):
        return 0
    try:
        enc = detect_encoding(path)
    except Exception as e:
        print(f"[WARN] failed to detect encoding for {path}: {e}")
        enc = None

    if not enc:
        enc = 'utf-8'

    count = 0
    with open(path, 'r', encoding=enc, errors='replace') as fh:
        it = iter(fh)
        line_no = 0
        pushback = None
        while True:
            try:
                if pushback is not None:
                    line = pushback
                    pushback = None
                else:
                    line = next(it)
            except StopIteration:
                break
            line_no += 1
            if TRAINING_KEY in line:
                # start a record
                start_line_no = line_no
                record_lines = [line.rstrip('\n')]
                # collect continuation lines
                while True:
                    try:
                        nxt = next(it)
                        line_no += 1
                    except StopIteration:
                        break
                    if is_log_header(nxt):
                        # this is next record's header -> pushback
                        pushback = nxt
                        line_no -= 1  # adjust because pushback will be processed again
                        break
                    # treat empty or whitespace-leading lines as continuation as well
                    record_lines.append(nxt.rstrip('\n'))
                # form combined message
                raw_first = record_lines[0]
                combined_message = '\n'.join(record_lines)
                timestamp = extract_timestamp(raw_first) or extract_timestamp(combined_message)
                level, tag, maybe_msg = extract_level_tag(raw_first)
                if not maybe_msg:
                    # fallback: message is the whole first line
                    maybe_msg = raw_first.strip()
                writer.writerow({
                    'timestamp': timestamp,
                    'level': level,
                    'tag': tag or TRAINING_KEY,
                    'message': maybe_msg,
                    'source_file': path,
                    'original_line_number': start_line_no,
                    'raw_line': raw_first,
                    'extraction_rule': 'contains_TrainingViewModel'
                })
                count += 1
    return count


def main():
    ap = argparse.ArgumentParser(description='Extract TrainingViewModel logs to CSV')
    ap.add_argument('--inputs', nargs='*', help='Input files to process (default: candidates)', default=[])
    ap.add_argument('--output', help='Output CSV path', default='extracted_logs/training_viewmodel_extracted.csv')
    ap.add_argument('--force-encoding', help='Force input encoding (skip detection)', default=None)
    args = ap.parse_args()

    inputs = args.inputs if args.inputs else DEFAULT_CANDIDATES
    # resolve only existing files
    existing = [p for p in inputs if os.path.isfile(p)]
    if not existing:
        print('[WARN] No input files found among candidates:', inputs)
        # also try to find any file in cwd that contains "device" or "recent_device_log"
        maybe = [p for p in os.listdir('.') if os.path.isfile(p) and ('device' in p or 'recent_device' in p or p.endswith('.log') or p.endswith('.txt'))]
        existing = maybe
        if not existing:
            print('[ERROR] No files to process. Exiting.')
            sys.exit(1)
        else:
            print('[INFO] Falling back to:', existing)

    out_dir = os.path.dirname(args.output)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    with open(args.output, 'w', newline='', encoding='utf-8') as csvf:
        fieldnames = ['timestamp', 'level', 'tag', 'message', 'source_file', 'original_line_number', 'raw_line', 'extraction_rule']
        writer = csv.DictWriter(csvf, fieldnames=fieldnames)
        writer.writeheader()
        total = 0
        for p in existing:
            print(f"[INFO] processing {p}...")
            try:
                c = process_file(p, writer)
                print(f"[INFO] found {c} matches in {p}")
                total += c
            except Exception as e:
                print(f"[ERROR] failed to process {p}: {e}")
        print(f"[DONE] total matches: {total}. csv saved to {args.output}")


if __name__ == '__main__':
    main()

