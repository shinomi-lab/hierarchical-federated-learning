"""Small E2E test helper.

What it does:
- Calls the Python terminal client to upload a small synthetic payload to the edge.
- Invokes central_server/verify_metadata_consistency.py to scan manifests and logs and prints a short summary.

Note: This is a helper for local testing. It expects the edge and central servers to be running locally
on their default ports (edge:8001, central:8000) or you can pass --edge/--central overrides.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TERMINAL_CLIENT = ROOT / "scripts" / "terminal_client.py"
VERIFY_SCRIPT = ROOT / "central_server" / "verify_metadata_consistency.py"


def run_terminal_client(edge: str, terminal_id: str, round_id: int, run_id: str | None = None):
    cmd = [sys.executable, str(TERMINAL_CLIENT), "--edge", edge, "--terminal-id", terminal_id, "--round", str(round_id), "--attempts", "4"]
    if run_id:
        cmd += ["--run-id", run_id]
    print("Running:", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    print("--- client stdout ---")
    print(p.stdout)
    print("--- client stderr ---")
    print(p.stderr)
    return p.returncode == 0


def run_verify(central: str):
    # the verify script reads received_files and logs; it doesn't need a central URL, but keep arg for future
    cmd = [sys.executable, str(VERIFY_SCRIPT)]
    print("Running verify:", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    print("--- verify stdout ---")
    print(p.stdout)
    print("--- verify stderr ---")
    print(p.stderr)
    return p.returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--edge", default="http://127.0.0.1:8001")
    parser.add_argument("--central", default="http://127.0.0.1:8000")
    parser.add_argument("--terminal-id", default="e2e-sim-01")
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    ok = run_terminal_client(args.edge, args.terminal_id, args.round, args.run_id)
    if not ok:
        print("Terminal client failed — aborting verify")
        sys.exit(1)

    # wait a moment for edge->central forwarding if enabled
    print("Waiting 2s for propagation...")
    time.sleep(2)

    ok = run_verify(args.central)
    if ok:
        print("Verify script ran — check stdout for details.")
    else:
        print("Verify script failed — see output above.")


if __name__ == "__main__":
    main()
