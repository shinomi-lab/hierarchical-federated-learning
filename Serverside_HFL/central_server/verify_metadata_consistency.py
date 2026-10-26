"""Simple CLI tool to verify manifest SHA/run_id appear in central logs.

Usage: python central_server/verify_metadata_consistency.py

Scans received_files/terminal_updates for manifest JSON files and checks
whether the manifested sha256 or run_id appear in any log files under
logs/time_records (a quick heuristic E2E check).
"""
import os
import glob
import json
from collections import defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MANIFEST_GLOB = os.path.join(ROOT, "..", "received_files", "terminal_updates", "**", "manifest_*.json")
LOG_GLOB = os.path.join(ROOT, "..", "logs", "time_records", "*.log")


def scan_manifests():
    files = glob.glob(MANIFEST_GLOB, recursive=True)
    out = []
    for p in files:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        sha = data.get("sha256") or data.get("sha")
        run_id = data.get("run_id") or data.get("run")
        out.append((p, sha, run_id))
    return out


def load_logs():
    texts = []
    for p in glob.glob(LOG_GLOB):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                texts.append((p, fh.read()))
        except Exception:
            continue
    return texts


def main():
    mans = scan_manifests()
    logs = load_logs()
    log_concat = "\n".join(t for _, t in logs)

    total = len(mans)
    sha_found = 0
    run_found = 0
    sha_missing = []
    run_missing = []

    for path, sha, run_id in mans:
        found_sha = False
        found_run = False
        if sha and sha in log_concat:
            found_sha = True
        if run_id and str(run_id) in log_concat:
            found_run = True
        if found_sha:
            sha_found += 1
        else:
            sha_missing.append((path, sha))
        if found_run:
            run_found += 1
        else:
            run_missing.append((path, run_id))

    print(f"Manifests scanned: {total}")
    print(f"SHA256 values found in logs: {sha_found} / {total}")
    print(f"run_id values found in logs: {run_found} / {total}")

    if sha_missing:
        print("\nSample manifests whose sha256 was NOT found in logs:")
        for p, s in sha_missing[:10]:
            print(f" - {p} -> {s}")

    if run_missing:
        print("\nSample manifests whose run_id was NOT found in logs:")
        for p, r in run_missing[:10]:
            print(f" - {p} -> {r}")

    print("\nNote: this is a heuristic check — absence in logs doesn't always mean the central server didn't record the item; consider searching received_edges and per-run meta files as well.")


if __name__ == "__main__":
    main()
