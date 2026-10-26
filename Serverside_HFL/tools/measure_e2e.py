import re
from pathlib import Path
import json

# Quick script to measure E2E latency from central aggregation -> central push -> edge receive
# Usage: python tools/measure_e2e.py --central logs/time_records/central_server_YYYYMMDD_*.log \
#                                   --edge logs/time_records/edge_server_YYYYMMDD_*.log
# It finds three events:
#  - aggregation_completed: {..., "next_round": N}
#  - push_to_edge: logged with edge and elapsed_s
#  - pushed_model_received: logged on edge with batch timestamp

import argparse
import datetime

CENTRAL_AGG_RE = re.compile(r"\[(?P<ts>[^\]]+)\].*aggregation_completed: (\{.*\})")
CENTRAL_PUSH_RE = re.compile(r"\[(?P<ts>[^\]]+)\].*push_to_edge: (\{.*\})")
EDGE_PUSHED_RE = re.compile(r"\[(?P<ts>[^\]]+)\].*pushed_model_received: (\{.*\})")


def parse_datetime(s):
    # time_logger format: YYYY-mm-dd HH:MM:SS.micro
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")
        except Exception:
            return None


def extract_events(log_path, pattern, json_group=2):
    events = []
    for line in Path(log_path).read_text(encoding='utf-8', errors='ignore').splitlines():
        m = pattern.search(line)
        if m:
            ts = m.group('ts')
            payload = m.group(json_group)
            try:
                data = json.loads(payload)
            except Exception:
                # fallback: try to eval-ish
                try:
                    data = json.loads(payload.replace("'", '"'))
                except Exception:
                    data = payload
            events.append((ts, data, line))
    return events


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--central', required=True)
    p.add_argument('--edge', required=True)
    args = p.parse_args()

    agg = extract_events(args.central, CENTRAL_AGG_RE)
    pushes = extract_events(args.central, CENTRAL_PUSH_RE)
    edge_recvs = extract_events(args.edge, EDGE_PUSHED_RE)

    print(f"Found {len(agg)} aggregation_completed, {len(pushes)} push_to_edge, {len(edge_recvs)} pushed_model_received")

    # map by nearest next_round or batch if present
    for a_ts, a_data, _ in agg[-10:]:
        a_time = parse_datetime(a_ts)
        print('\nAggregation at', a_time, a_data)
        # find first push after this aggregation
        push_after = None
        for p_ts, p_data, _ in pushes:
            p_time = parse_datetime(p_ts)
            if p_time and a_time and p_time >= a_time:
                push_after = (p_time, p_data)
                break
        if push_after:
            print('  Push at', push_after[0], push_after[1])
            # find edge receive after push
            recv_after = None
            for e_ts, e_data, _ in edge_recvs:
                e_time = parse_datetime(e_ts)
                if e_time and e_time >= push_after[0]:
                    recv_after = (e_time, e_data)
                    break
            if recv_after:
                delta = (recv_after[0] - a_time).total_seconds()
                print('  Edge received at', recv_after[0], 'E2E seconds:', delta)
            else:
                print('  No edge receive found after push')
        else:
            print('  No push found after aggregation')

if __name__ == '__main__':
    main()
