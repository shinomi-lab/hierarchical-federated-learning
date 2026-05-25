import sys
import subprocess
from pathlib import Path
import argparse

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
ANALYSIS = ROOT / "logs" / "analysis"


def run_collect_events(day: str | None, latest: bool, outdir: Path, fmt: str, merge_all: bool) -> int:
    cmd = [
        sys.executable,
        str(TOOLS / "collect_events.py"),
        "--outdir",
        str(outdir),
        "--format",
        fmt,
    ]
    if merge_all:
        cmd.append("--merge-all")
    if latest:
        cmd.append("--latest")
    if day:
        cmd.extend(["--day", day])
    print(f"[rollup] Running: {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def run_aggregate_training(day: str | None, prefix: str) -> int:
    cmd = [
        sys.executable,
        str(TOOLS / "aggregate_training_data.py"),
        "--prefix",
        prefix,
    ]
    if day:
        cmd.extend(["--day", day])
    print(f"[rollup] Running: {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def main():
    parser = argparse.ArgumentParser(description="Run shared-storage rollup: events + training data aggregation")
    parser.add_argument("--day", help="YYYYMMDD; if omitted, use --latest or all", default=None)
    parser.add_argument("--latest", help="Use latest event day when --day is not set", action="store_true")
    parser.add_argument("--outdir", help="Output directory for event files", default=str(ANALYSIS))
    parser.add_argument("--format", help="Event output format: csv,json,both", default="both")
    parser.add_argument("--prefix", help="Prefix for training aggregation outputs", default="aggregate")
    parser.add_argument("--no-merge-all", help="Do not produce merged events_all output", action="store_true")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    rc1 = run_collect_events(day=args.day, latest=args.latest, outdir=outdir, fmt=args.format, merge_all=(not args.no_merge_all))
    rc2 = run_aggregate_training(day=args.day, prefix=args.prefix)

    if rc1 == 0 and rc2 == 0:
        print("[rollup] Complete. Outputs in:", outdir)
        sys.exit(0)
    else:
        print(f"[rollup] Failed: collect_events rc={rc1}, aggregate_training rc={rc2}")
        sys.exit(1)


if __name__ == "__main__":
    main()
