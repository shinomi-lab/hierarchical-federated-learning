#!/usr/bin/env python3
"""Auto-convert .pt model files in received_files to weight.bin + meta.json.

Usage examples:
  python tools/auto_convert_batches.py                # scan default received_files and convert missing
  python tools/auto_convert_batches.py --batch 20251217_195228  # convert single batch
  python tools/auto_convert_batches.py --notify-url http://127.0.0.1:8000/api/v1/push_model_to_devices

The script invokes the existing `tools/pt_to_meta_weights.py` script using the same
Python interpreter, captures stdout/stderr and emits JSON-ish log lines to stdout so
the edge server log format can be correlated.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Iterable

DEFAULT_ROOT = Path(__file__).resolve().parents[0].parents[0] / "received_files"
CONVERTER = Path(__file__).resolve().parent / "pt_to_meta_weights.py"


def find_batches(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        pt = p / "global_model_mobile.pt"
        binf = p / "weight.bin"
        metaf = p / "meta.json"
        if pt.exists() and (not binf.exists() or not metaf.exists()):
            yield p


def run_converter(pt_path: Path, timeout: int = 300) -> dict:
    # pt_to_meta_weights.py expects two args: <pt> <out_dir>
    cmd = [sys.executable, str(CONVERTER), str(pt_path), str(pt_path.parent)]
    batch = pt_path.parent.name
    logging.info(json.dumps({"event": "auto_convert_started", "details": {"batch": batch, "model": str(pt_path)}}))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rc = proc.returncode
        if rc == 0:
            logging.info(json.dumps({"event": "auto_converted", "details": {"batch": batch, "stdout": proc.stdout}}))
            return {"batch": batch, "rc": rc, "stdout": proc.stdout, "stderr": proc.stderr}
        else:
            logging.error(json.dumps({"event": "auto_convert_failed", "details": {"batch": batch, "rc": rc, "stderr": proc.stderr}}))
            return {"batch": batch, "rc": rc, "stdout": proc.stdout, "stderr": proc.stderr}
    except Exception as exc:
        logging.exception("converter exception")
        return {"batch": batch, "rc": -1, "stdout": "", "stderr": str(exc)}


def notify_push(url: str, batch: str) -> None:
    # send a minimal JSON body to the push endpoint; swallow errors but log them
    body = {"batch": batch}
    try:
        # prefer httpx if available, else requests, else urllib
        try:
            import httpx

            r = httpx.post(url, json=body, timeout=5.0)
            logging.info(json.dumps({"event": "notify_posted", "details": {"batch": batch, "url": url, "status_code": r.status_code}}))
            return
        except Exception:
            pass
        try:
            import requests

            r = requests.post(url, json=body, timeout=5.0)
            logging.info(json.dumps({"event": "notify_posted", "details": {"batch": batch, "url": url, "status_code": r.status_code}}))
            return
        except Exception:
            pass
        # fallback
        from urllib import request

        req = request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=5.0) as resp:
            logging.info(json.dumps({"event": "notify_posted", "details": {"batch": batch, "url": url, "status_code": resp.getcode()}}))
    except Exception as exc:
        logging.error(json.dumps({"event": "notify_failed", "details": {"batch": batch, "url": url, "error": str(exc)}}))


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=str(DEFAULT_ROOT), help="received_files root")
    p.add_argument("--batch", action="append", help="specific batch name to convert (can be repeated)")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--notify-url", help="optional URL to POST after successful conversion (push endpoint)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    root = Path(args.root)
    if args.batch:
        batches = [root / b for b in args.batch]
    else:
        batches = list(find_batches(root))

    if not batches:
        logging.info(json.dumps({"event": "nothing_to_convert", "details": {"root": str(root)}}))
        return 0

    logging.info(json.dumps({"event": "convert_plan", "details": {"n": len(batches), "dry_run": bool(args.dry_run)}}))

    if args.dry_run:
        for b in batches:
            logging.info(json.dumps({"event": "would_convert", "details": {"batch": b.name, "pt": str(b / "global_model_mobile.pt")}}))
        return 0

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(run_converter, b / "global_model_mobile.pt"): b for b in batches}
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result()
            results.append(res)
            if res.get("rc") == 0 and args.notify_url:
                notify_push(args.notify_url, res["batch"])

    # summary
    ok = sum(1 for r in results if r.get("rc") == 0)
    fail = len(results) - ok
    logging.info(json.dumps({"event": "convert_summary", "details": {"total": len(results), "ok": ok, "fail": fail}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
