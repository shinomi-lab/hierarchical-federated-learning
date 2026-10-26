"""
Terminal client helper for uploading aggregated weights with robust resend-guard.

Features:
- persistent sqlite store of sent requests (req_id, sha, round, ack_timestamp)
- generates a UUID req_id per upload
- check server markers before sending (if server supports /api/markers)
- exponential backoff retry only on network errors or 5xx responses
- records successful ack into local DB so restarts don't resend

Usage:
  python scripts/terminal_client.py send --url http://central:8000/edge_update --file ./agg.pt --edge edge-server-01 --round 27 --num-clients 1 --sum-n-samples 100

This is a client-side tool intended as a reference implementation for terminals.
"""
import argparse
import sqlite3
from pathlib import Path
import uuid
import time
import requests
import os
import hashlib
import json


DB_PATH = Path(__file__).resolve().parent / 'terminal_client_store.sqlite'


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.cursor()
        cur.execute('''
        CREATE TABLE IF NOT EXISTS sent_requests (
            req_id TEXT PRIMARY KEY,
            sha TEXT,
            round INTEGER,
            edge_id TEXT,
            file_path TEXT,
            ack_timestamp TEXT
        )
        ''')
        conn.commit()
    finally:
        conn.close()


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(8192), b''):
            h.update(chunk)
    return 'sha256:' + h.hexdigest()


def already_sent(sha: str) -> bool:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.cursor()
        cur.execute('SELECT ack_timestamp FROM sent_requests WHERE sha = ?', (sha,))
        r = cur.fetchone()
        return r is not None
    finally:
        conn.close()


def mark_sent(req_id: str, sha: str, round_num: int, edge_id: str, file_path: str, ack_ts: str):
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.cursor()
        cur.execute('INSERT OR REPLACE INTO sent_requests (req_id, sha, round, edge_id, file_path, ack_timestamp) VALUES (?,?,?,?,?,?)',
                    (req_id, sha, int(round_num), edge_id, file_path, ack_ts))
        conn.commit()
    finally:
        conn.close()


def check_server_has_sha(server_base: str, sha: str, timeout=3.0) -> bool:
    try:
        url = server_base.rstrip('/') + f'/api/markers/hash/{sha}'
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            j = r.json()
            return bool(j.get('exists'))
    except Exception:
        pass
    return False


def send_with_retry(server_url: str, file_path: Path, edge_id: str, round_num: int, num_clients: int, sum_n_samples: int, max_retries=5):
    init_db()
    session = requests.Session()
    # measure file read + sha calculation (use larger chunk to speed up)
    t0 = time.perf_counter()
    sha = sha256_of_file(file_path)
    t_sha = time.perf_counter() - t0
    print(json.dumps({"event": "client_file_sha", "file": str(file_path), "sha": sha, "duration_s": round(t_sha, 6)}))
    # quick local guard
    if already_sent(sha):
        print('Already sent and acked locally for sha', sha)
        return True

    # remote guard: ask server if it already knows this sha (measure)
    parsed = requests.utils.urlparse(server_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    t_check0 = time.perf_counter()
    server_has = check_server_has_sha(base, sha)
    t_check = time.perf_counter() - t_check0
    print(json.dumps({"event": "client_server_check", "sha": sha, "server_has": server_has, "duration_s": round(t_check, 6)}))
    if server_has:
        print('Server reports sha already received; marking locally and skipping upload')
        # still mark as acked without req_id
        mark_sent(str(uuid.uuid4()), sha, round_num, edge_id, str(file_path), time.strftime('%Y%m%d_%H%M%S'))
        return True

    req_id = str(uuid.uuid4())
    # prepare file object; we'll seek after sha calculation
    fh = open(file_path, 'rb')
    try:
        fh.seek(0)
    except Exception:
        pass
    files = {'weights': (file_path.name, fh, 'application/octet-stream')}
    data = {
        'edge_id': edge_id,
        'round': str(round_num),
        'num_clients': str(num_clients),
        'sum_n_samples': str(sum_n_samples),
        'content_sha256': sha,
        'run_id': req_id,
    }

    attempt = 0
    backoff = 1.0
    while attempt <= max_retries:
        try:
            print(f'Attempt {attempt+1} sending to {server_url} (req_id={req_id})')
            # measure request time using persistent session (keep-alive)
            t_req0 = time.perf_counter()
            r = session.post(server_url, data=data, files=files, timeout=120)
            t_req = time.perf_counter() - t_req0
            try:
                # ensure file descriptor is closed per attempt
                files['weights'][1].close()
            except Exception:
                pass
            print(json.dumps({"event": "client_request", "req_id": req_id, "status_code": getattr(r, 'status_code', None), "duration_s": round(t_req, 6)}))
            if r.status_code == 200:
                j = r.json()
                if j.get('ack'):
                    ack_ts = j.get('ack_timestamp') or time.strftime('%Y%m%d_%H%M%S')
                    mark_sent(req_id, sha, round_num, edge_id, str(file_path), ack_ts)
                    print('Upload acknowledged at', ack_ts)
                    return True
                else:
                    print('Server returned non-ack response:', j)
                    # if server asked to retry, treat as retryable
            elif 500 <= r.status_code < 600:
                print('Server error', r.status_code, '; will retry')
            else:
                print('Client error or unexpected status', r.status_code, r.text)
                return False
        except requests.exceptions.RequestException as e:
            print('Network error:', e)
        attempt += 1
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)
        # reopen file for next attempt
        try:
            fh = open(file_path, 'rb')
            files = {'weights': (file_path.name, fh, 'application/octet-stream')}
        except Exception:
            pass

    print('Exceeded max retries; giving up')
    return False


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd')
    s = sub.add_parser('send')
    s.add_argument('--url', required=True)
    s.add_argument('--file', required=True)
    s.add_argument('--edge', required=True)
    s.add_argument('--round', required=True, type=int)
    s.add_argument('--num-clients', required=True, type=int)
    s.add_argument('--sum-n-samples', required=True, type=int)
    args = p.parse_args()

    if args.cmd == 'send':
        fp = Path(args.file)
        if not fp.exists():
            print('file not found:', fp)
            return 2
        ok = send_with_retry(args.url, fp, args.edge, args.round, args.num_clients, args.sum_n_samples)
        if ok:
            print('Done')
            return 0
        else:
            print('Failed')
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
