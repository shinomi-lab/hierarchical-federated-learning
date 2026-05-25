#!/usr/bin/env python3
"""簡易再現スクリプト: 1プロセスから5ラウンド連続で aggregate を呼び、pending/index の挙動を観察する

使い方:
  python scripts/repro_5_rounds.py
"""
import asyncio
import sys
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import torch
from edge_server.state import UpdateRecord, append_update, state_dict_lock, terminal_state_dicts
from edge_server.endpoints import aggregation
from edge_server.config import AGGREGATION_CACHE_DIR


async def add_dummy_updates_for_round(round_id: int, num_updates: int = 2):
    async with state_dict_lock:
        for j in range(num_updates):
            tid = f"terminal_{round_id}_{j}"
            sd = {"w": torch.tensor([float(round_id)], dtype=torch.float32)}
            rec = UpdateRecord(terminal_id=tid, state_dict=sd, n_samples=1, sig=f"sha-{round_id}-{j}", path=f"/tmp/agg_{round_id}_{j}.pt")
            append_update(round_id, rec)


async def run_sequence():
    print("Start repro: 5 consecutive rounds (no network send)")
    AGGREGATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # ensure pending index loaded
    aggregation._load_pending_index_from_disk()

    for r in range(1, 6):
        print(f"--- Round {r}: adding updates")
        await add_dummy_updates_for_round(r, num_updates=2)
        # call aggregation handler but with auto_send=False to avoid network
        print(f"Calling aggregate_and_send_to_central_server for round {r} (auto_send=False)")
        await aggregation.aggregate_and_send_to_central_server(edge_id="edge_test", round_id=r, model_id=f"m-{r}", auto_send=False)
        # short pause
        time.sleep(0.2)
        # print diagnostics
        async with aggregation._pending_index_lock:
            pending = list(aggregation._pending_index.keys())
        try:
            async with aggregation._sent_rounds_lock:
                sent = list(aggregation.sent_rounds)
        except Exception:
            sent = list(aggregation.sent_rounds)
        print(f"After round {r}: pending_count={len(pending)} pending_sample={pending[:5]} sent_count={len(sent)} sent_sample={sent[:5]}")

    print("Repro finished. Aggregation cache dir:", AGGREGATION_CACHE_DIR)
    print("List files:")
    for p in sorted(AGGREGATION_CACHE_DIR.rglob("*")):
        print(" ", p.relative_to(AGGREGATION_CACHE_DIR))


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--auto', action='store_true', help='enable auto_send when invoking aggregation (will attempt HTTP POST to central)')
    args = p.parse_args()
    if args.auto:
        # call aggregation with auto_send=True by wrapping the loop
        async def run_auto():
            print('Running with auto_send=True (network calls will be attempted)')
            AGGREGATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            aggregation._load_pending_index_from_disk()
            for r in range(1, 6):
                await add_dummy_updates_for_round(r, num_updates=2)
                print(f'Round {r}: aggregating with auto_send=True')
                await aggregation.aggregate_and_send_to_central_server(edge_id='edge_test', round_id=r, model_id=f'm-{r}', auto_send=True)
                await asyncio.sleep(0.2)
                async with aggregation._pending_index_lock:
                    pending = list(aggregation._pending_index.keys())
                try:
                    async with aggregation._sent_rounds_lock:
                        sent = list(aggregation.sent_rounds)
                except Exception:
                    sent = list(aggregation.sent_rounds)
                print(f'After round {r}: pending_count={len(pending)} sent_count={len(sent)}')
        asyncio.run(run_auto())
    else:
        asyncio.run(run_sequence())
