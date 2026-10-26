"""
Profiler runner for Edge aggregation path.

Creates synthetic UpdateRecord entries in `edge_server.state` and invokes
`aggregate_and_send_to_central_server` under cProfile. Writes profiling report
and a human-readable top-20 callees to `tools/profiler_report.txt`.

Usage:
  python tools/run_edge_profiler.py --round 1 --n-terminals 5 --tensor-size 10000

Notes:
 - This runner calls the edge aggregation code directly (no HTTP required).
 - By default `auto_send=False` to avoid network calls; set `--auto-send` to True
   if you want to exercise the HTTP send path (requires CENTRAL_SERVER_URL reachable).
"""
from __future__ import annotations

import argparse
import asyncio
import cProfile
import pstats
import io
import time
from pathlib import Path

import torch

# Import the modules under test
from edge_server.endpoints import aggregation
from edge_server import state as edge_state
from edge_server.state import UpdateRecord

OUT = Path(__file__).resolve().parent


def make_dummy_state_dict(size: int):
    # Create a single flat tensor parameter to simulate model weights
    return {"w": torch.randn(size, dtype=torch.float32)}


async def run_profile(round_id: int, n_terminals: int, tensor_size: int, auto_send: bool):
    # prepare synthetic updates
    edge_state.terminal_state_dicts.pop(round_id, None)
    for i in range(n_terminals):
        tid = f"term_{i+1}"
        sd = make_dummy_state_dict(tensor_size)
        rec = UpdateRecord(
            terminal_id=tid,
            state_dict=sd,
            n_samples=100,
            sig=f"sha256:dummy{i}",
            path=str(Path.cwd() / f"dummy_{i}.pt"),
            manifest=None,
            run_id=None,
            local_seq=i,
            started_at=None,
            event_ts=None,
        )
        edge_state.append_update(round_id, rec)

    # Run aggregation under profile
    pr = cProfile.Profile()
    pr.enable()
    t0 = time.time()
    # call the async aggregator
    await aggregation.aggregate_and_send_to_central_server(edge_state.current_edge_state.model_id, round_id, model_id=None, auto_send=auto_send)
    elapsed = time.time() - t0
    pr.disable()

    # dump stats
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
    ps.print_stats(50)
    # write raw profile
    prof_path = OUT / "profiler_output.prof"
    pr.dump_stats(str(prof_path))
    rpt_path = OUT / "profiler_report.txt"
    rpt_path.write_text(s.getvalue(), encoding="utf-8")
    # also write a short summary
    summary = []
    summary.append(f"Aggregate round={round_id} with {n_terminals} terminals, tensor_size={tensor_size}")
    summary.append(f"Elapsed seconds: {elapsed:.3f}")
    summary.append("Top callers (by cumulative):")
    out = io.StringIO()
    ps = pstats.Stats(pr, stream=out).sort_stats("cumulative")
    ps.print_callees(20)
    summary.append(out.getvalue())
    (OUT / "profiler_summary.txt").write_text("\n".join(summary), encoding="utf-8")
    print(f"Profile run complete. elapsed={elapsed:.3f}s")
    print(f"Wrote: {prof_path}, {rpt_path}, {OUT / 'profiler_summary.txt'}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--round", type=int, default=1)
    p.add_argument("--n-terminals", type=int, default=5)
    p.add_argument("--tensor-size", type=int, default=100000)
    p.add_argument("--auto-send", action="store_true")
    args = p.parse_args()

    asyncio.run(run_profile(args.round, args.n_terminals, args.tensor_size, args.auto_send))


if __name__ == '__main__':
    main()
