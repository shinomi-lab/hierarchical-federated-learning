import json
import csv
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import argparse
import glob

ROOT = Path(__file__).resolve().parent.parent
EVENTS_DIR = ROOT / "logs" / "events"
ROUNDS_SUMMARY = ROOT / "logs" / "rounds" / "round_summary.jsonl"
DEFAULT_OUT_DIR = ROOT / "logs" / "analysis"


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not path.exists():
        return items
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                pass
    return items


def collect_events(day: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for comp_dir in EVENTS_DIR.glob("*/"):
        comp = comp_dir.name
        out.setdefault(comp, [])
        if day:
            paths = [comp_dir / f"{day}.jsonl"]
        else:
            paths = [Path(p) for p in glob.glob(str(comp_dir / "*.jsonl"))]
        for p in paths:
            rows = read_jsonl(p)
            for r in rows:
                # 補助フィールド：どのコンポーネントのイベントかを明示
                if "component" not in r:
                    r["component"] = comp
            out[comp].extend(rows)
    return out


def list_event_days() -> List[str]:
    days: set[str] = set()
    for comp_dir in EVENTS_DIR.glob("*/"):
        for p in comp_dir.glob("*.jsonl"):
            name = p.stem  # YYYYMMDD を想定
            if len(name) == 8 and name.isdigit():
                days.add(name)
    return sorted(days)


def latest_event_day() -> Optional[str]:
    days = list_event_days()
    return days[-1] if days else None


def write_csv(path: Path, rows: List[Dict[str, Any]]):
    if not rows:
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    parser = argparse.ArgumentParser(description="Collect structured events and round summaries")
    parser.add_argument("--day", help="YYYYMMDD to restrict collection", default=None)
    parser.add_argument("--latest", help="Use latest available day from events", action="store_true")
    parser.add_argument("--outdir", help="Output directory", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--format", help="Output format: csv,json,both", default="both")
    parser.add_argument("--merge-all", help="Also write merged events across all components", action="store_true")
    args = parser.parse_args()

    # dayの決定ロジック
    day: Optional[str] = args.day
    if args.latest and not day:
        day = latest_event_day()

    out_dir = Path(args.outdir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events = collect_events(day)

    def dump(name: str, rows: List[Dict[str, Any]]):
        if args.format in ("csv", "both"):
            write_csv(out_dir / f"{name}_{day or 'all'}.csv", rows)
        if args.format in ("json", "both"):
            with open(out_dir / f"{name}_{day or 'all'}.json", "w", encoding="utf-8") as jf:
                json.dump(rows, jf, ensure_ascii=False, indent=2)

    # コンポーネント別出力
    for comp, rows in events.items():
        dump(f"events_{comp}", rows)

    # 全コンポーネント横断の結合出力
    if args.merge_all:
        merged: List[Dict[str, Any]] = []
        for comp, rows in events.items():
            for r in rows:
                if "component" not in r:
                    r["component"] = comp
            merged.extend(rows)
        dump("events_all", merged)

    # ラウンドサマリー
    rounds = read_jsonl(ROUNDS_SUMMARY)
    dump("rounds_summary", rounds)

    # サマリ表示
    print("Collected:")
    for comp in events.keys():
        print(f" - {comp}: {len(events[comp])} events")
    if args.merge_all:
        total = sum(len(v) for v in events.values())
        print(f" - all: {total} events")
    print(f" - rounds: {len(rounds)} entries")

if __name__ == "__main__":
    main()
