import os
import sys
import time
import json
import signal
import argparse
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional

ROOT = Path(__file__).resolve().parent.parent
LOG_EVENTS_DIR = ROOT / "logs" / "events"
ROUND_SUMMARY = ROOT / "logs" / "rounds" / "round_summary.jsonl"

PROC_LIST: List[subprocess.Popen] = []


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not path.exists():
        return items
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return items


def summarize_events(day: Optional[str] = None) -> Dict[str, Any]:
    comps = ["edge_client_logs", "edge_terminal_update", "central_edge_aggregation"]
    out: Dict[str, Any] = {}
    for comp in comps:
        p = LOG_EVENTS_DIR / comp / f"{day or time.strftime('%Y%m%d')}.jsonl"
        rows = read_jsonl(p)
        out[comp] = {
            "count": len(rows),
            "last": rows[-1] if rows else None,
        }
    rs = read_jsonl(ROUND_SUMMARY)
    out["rounds"] = {
        "count": len(rs),
        "last": rs[-1] if rs else None,
    }
    return out


def print_summary(day: Optional[str] = None):
    s = summarize_events(day)
    print("\n=== Quick Summary ===")
    for k in ["edge_client_logs", "edge_terminal_update", "central_edge_aggregation"]:
        v = s.get(k, {})
        print(f"- {k}: {v.get('count', 0)} events")
    r = s.get("rounds", {})
    print(f"- rounds: {r.get('count', 0)} entries")
    last_r = r.get("last")
    if last_r:
        print(f"  last round entry -> round={last_r.get('round')} edges_received={last_r.get('edges_received')} expected={last_r.get('expected_edges')} time={last_r.get('time')}")
    print("====================\n")


def start_process(cmd: List[str], env: Dict[str, str]) -> subprocess.Popen:
    print(f"[RUN] {' '.join(cmd)}")
    p = subprocess.Popen(cmd, env={**os.environ, **env})
    PROC_LIST.append(p)
    return p


def stop_all():
    for p in PROC_LIST:
        try:
            p.terminate()
        except Exception:
            pass
    time.sleep(0.5)
    for p in PROC_LIST:
        try:
            p.kill()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="One-click experiment launcher and summarizer")
    parser.add_argument("--edges", type=int, default=1, choices=[1,2], help="number of edges to start")
    parser.add_argument("--central-port", type=int, default=8000)
    parser.add_argument("--edge1-port", type=int, default=8001)
    parser.add_argument("--edge2-port", type=int, default=8002)
    parser.add_argument("--edge1-threshold", type=int, default=1, choices=[1,2])
    parser.add_argument("--edge2-threshold", type=int, default=1, choices=[1,2])
    parser.add_argument("--allowlist-json", type=str, default=None, help="TERMINAL_ALLOWLIST_JSON override")
    parser.add_argument("--summary-interval", type=int, default=20, help="seconds between quick summaries")
    parser.add_argument("--only-summary", action="store_true", help="do not start servers, only print summaries periodically")
    args = parser.parse_args()

    def handle_sig(signum, frame):
        print("\n[STOP] Stopping processes...")
        stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    if args.only_summary:
        try:
            while True:
                print_summary()
                time.sleep(args.summary_interval)
        except KeyboardInterrupt:
            print("bye")
        return

    # Start central
    central_env = {}
    if args.allowlist_json:
        central_env["TERMINAL_ALLOWLIST_JSON"] = args.allowlist_json
    start_process([
        sys.executable, "-m", "uvicorn", "central_server.main:app",
        "--host", "0.0.0.0", "--port", str(args.central_port)
    ], env=central_env)

    time.sleep(1.0)
    # Start edge 1
    edge1_env = {
        "EDGE_SERVER_ID": "edge-server-01",
        "EDGE_URL": f"http://127.0.0.1:{args.edge1_port}",
        "CENTRAL_SERVER_URL": f"http://127.0.0.1:{args.central_port}",
        "EDGE_AGGREGATION_THRESHOLD": str(args.edge1_threshold),
        "EDGE_AGGREGATION_THRESHOLD_OVERRIDE": str(args.edge1_threshold),
    }
    if args.allowlist_json:
        edge1_env["TERMINAL_ALLOWLIST_JSON"] = args.allowlist_json
    start_process([
        sys.executable, "-m", "uvicorn", "edge_server.main:app",
        "--host", "0.0.0.0", "--port", str(args.edge1_port)
    ], env=edge1_env)

    # Start edge 2 if requested
    if args.edges == 2:
        edge2_env = {
            "EDGE_SERVER_ID": "edge-server-02",
            "EDGE_URL": f"http://127.0.0.1:{args.edge2_port}",
            "CENTRAL_SERVER_URL": f"http://127.0.0.1:{args.central_port}",
            "EDGE_AGGREGATION_THRESHOLD": str(args.edge2_threshold),
            "EDGE_AGGREGATION_THRESHOLD_OVERRIDE": str(args.edge2_threshold),
        }
        if args.allowlist_json:
            edge2_env["TERMINAL_ALLOWLIST_JSON"] = args.allowlist_json
        start_process([
            sys.executable, "-m", "uvicorn", "edge_server.main:app",
            "--host", "0.0.0.0", "--port", str(args.edge2_port)
        ], env=edge2_env)

    print("\n[INFO] Servers started. Press Ctrl+C to stop.")
    print("[INFO] Printing quick summaries. Adjust interval with --summary-interval.")

    try:
        while True:
            print_summary()
            time.sleep(args.summary_interval)
    except KeyboardInterrupt:
        handle_sig(signal.SIGINT, None)


if __name__ == "__main__":
    main()
