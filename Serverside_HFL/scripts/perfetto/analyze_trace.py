#!/usr/bin/env python3
"""
Perfetto/Atraceトレースから精度低下分析用メトリクスを抽出する完全版スクリプト。

追加抽出する変数:
  - lmk_kill_count: Low Memory Killerによるプロセス強制終了イベント数（メモリ枯渇の証拠）
  - binder_transaction_count: HwBinder/Binder通信の発生回数（通信リソース競合の証拠）
  - txtファイルへの対応: .pftraceだけでなく、解凍済みの.txtログも直接解析可能

使い方:
  python3 analyze_trace.py trace_11_20260506_1724.txt
  python3 analyze_trace.py ./traces_dir/ --out results.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List

# ── Perfetto SQL クエリ定義 ───────────────────────────────────────────────

SQL_CONTEXT_SWITCHES = """
SELECT count(*) as switch_count
FROM sched_slice
WHERE utid IN (
    SELECT utid FROM thread WHERE upid IN (
        SELECT upid FROM process WHERE name LIKE '%hfl_experiment%'
    )
)
"""

SQL_OTHER_APP_CPU_TIME = """
SELECT
    process.name as proc_name,
    SUM(sched_slice.dur) / 1000000.0 as cpu_time_ms
FROM sched_slice
JOIN thread USING(utid)
JOIN process USING(upid)
WHERE process.name NOT LIKE '%hfl_experiment%'
    AND process.name NOT LIKE '%traced%'
    AND process.name NOT LIKE '%perfetto%'
    AND sched_slice.dur > 0
GROUP BY process.name
ORDER BY cpu_time_ms DESC
LIMIT 20
"""

SQL_CPU_FREQ = """
SELECT
    cpu,
    MAX(value) as max_freq_khz,
    MIN(value) as min_freq_khz,
    (MAX(value) - MIN(value)) / 1000.0 as drop_mhz
FROM counter
JOIN counter_track ON counter.track_id = counter_track.id
WHERE counter_track.name = 'cpufreq'
GROUP BY cpu
"""

SQL_NET_XMIT = """
SELECT count(*) as xmit_count
FROM ftrace_event
WHERE name = 'net_dev_xmit'
"""

SQL_HFL_SLICES = """
SELECT
    slice.name,
    slice.dur / 1000000.0 as dur_ms,
    slice.ts / 1000000.0 as start_ms
FROM slice
JOIN thread_track ON slice.track_id = thread_track.id
JOIN thread USING(utid)
JOIN process USING(upid)
WHERE process.name LIKE '%hfl_experiment%'
    AND slice.name LIKE 'HFL_%'
ORDER BY slice.ts
"""

# 【新規追加】メモリ枯渇の証拠（Low Memory Killer）
SQL_LMK_EVENTS = """
SELECT count(*) as lmk_count
FROM ftrace_event
WHERE name LIKE '%lowmemorykiller%' OR name LIKE '%oom%'
"""

# 【新規追加】システム間通信の競合（Binder/HwBinder）
SQL_BINDER_TRANSACTIONS = """
SELECT count(*) as binder_count
FROM slice
WHERE name LIKE '%Binder%' OR name LIKE '%binder%'
"""


@dataclass
class TraceMetrics:
    device_ip: str = ""
    trace_path: str = ""
    interrupt_count: int = 0
    other_app_cpu_time_ms: float = 0.0
    other_app_top3: List[Dict[str, float]] = field(default_factory=list)
    thermal_throttling_drop_mhz: float = 0.0
    max_freq_mhz: float = 0.0
    min_freq_mhz: float = 0.0
    net_xmit_count: int = 0
    lmk_kill_count: int = 0               # 追加
    binder_transaction_count: int = 0     # 追加
    hfl_total_training_ms: float = 0.0
    hfl_total_upload_ms: float = 0.0
    hfl_cycle_durations_ms: List[float] = field(default_factory=list)


def run_trace_processor_query(trace_path: Path, sql: str) -> List[List]:
    try:
        from perfetto.trace_processor import TraceProcessor
        tp = TraceProcessor(trace=str(trace_path))
        result = tp.query(sql)
        rows = [list(row.values()) for row in result]
        tp.close()
        return rows
    except ImportError:
        pass

    try:
        cmd = ["trace_processor_shell", "--query", sql, str(trace_path)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return []
        lines = r.stdout.strip().split("\n")
        if len(lines) <= 1:
            return []
        rows = []
        for line in lines[1:]:
            if line.strip():
                rows.append(line.split(",") if "," in line else line.split("\t"))
        return rows
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("WARN: trace_processor not available. Install with: pip install perfetto", file=sys.stderr)
        return []


def analyze_trace(trace_path: Path) -> TraceMetrics:
    metrics = TraceMetrics(trace_path=str(trace_path))
    stem = trace_path.stem
    metrics.device_ip = stem.replace("_trace", "")

    # Context switches
    rows = run_trace_processor_query(trace_path, SQL_CONTEXT_SWITCHES)
    if rows and rows[0]:
        metrics.interrupt_count = int(rows[0][0])

    # Other app CPU time
    rows = run_trace_processor_query(trace_path, SQL_OTHER_APP_CPU_TIME)
    for row in rows:
        try:
            name, ms = str(row[0]), float(row[1])
            metrics.other_app_cpu_time_ms += ms
            if len(metrics.other_app_top3) < 3:
                metrics.other_app_top3.append({"process": name, "cpu_time_ms": ms})
        except (ValueError, IndexError):
            continue

    # Thermal throttling
    rows = run_trace_processor_query(trace_path, SQL_CPU_FREQ)
    global_min = float("inf")
    for row in rows:
        try:
            max_khz, min_khz, drop = float(row[1]), float(row[2]), float(row[3])
            metrics.thermal_throttling_drop_mhz = max(metrics.thermal_throttling_drop_mhz, drop)
            metrics.max_freq_mhz = max(metrics.max_freq_mhz, max_khz / 1000.0)
            global_min = min(global_min, min_khz / 1000.0)
        except (ValueError, IndexError):
            continue
    metrics.min_freq_mhz = global_min if global_min != float("inf") else 0.0

    # LMK Events (New)
    rows = run_trace_processor_query(trace_path, SQL_LMK_EVENTS)
    if rows and rows[0]:
        metrics.lmk_kill_count = int(rows[0][0])

    # Binder Transactions (New)
    rows = run_trace_processor_query(trace_path, SQL_BINDER_TRANSACTIONS)
    if rows and rows[0]:
        metrics.binder_transaction_count = int(rows[0][0])

    # Network / HFL Slices
    rows = run_trace_processor_query(trace_path, SQL_NET_XMIT)
    if rows and rows[0]:
        metrics.net_xmit_count = int(rows[0][0])

    rows = run_trace_processor_query(trace_path, SQL_HFL_SLICES)
    for row in rows:
        try:
            name, dur = str(row[0]), float(row[1])
            if "runFiveCycles" in name:
                metrics.hfl_total_training_ms = dur
            elif "upload" in name.lower():
                metrics.hfl_total_upload_ms += dur
            elif "cycle_" in name:
                metrics.hfl_cycle_durations_ms.append(dur)
        except (ValueError, IndexError):
            continue

    return metrics


def compute_straggler_penalty(all_metrics: List[TraceMetrics]) -> Dict[str, float]:
    training_times = {m.device_ip: m.hfl_total_training_ms for m in all_metrics if m.hfl_total_training_ms > 0}
    if not training_times:
        return {}
    max_time = max(training_times.values())
    return {ip: (max_time - t) / max_time if max_time > 0 else 0.0 for ip, t in training_times.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace_target", type=str, help="解析するディレクトリ、または単一のファイル(.txt, .pftrace)")
    ap.add_argument("--out", type=str, default=None, help="出力JSONファイルパス")
    args = ap.parse_args()

    target = Path(args.trace_target)
    traces = []
    
    if target.is_file():
        traces.append(target)
    elif target.is_dir():
        # .txt 拡張子の生ログファイルも処理対象に追加
        traces = sorted(target.glob("*.pftrace")) + sorted(target.glob("*.perfetto_trace")) + sorted(target.glob("*.txt"))

    if not traces:
        print("Error: 有効なトレースファイルが見つかりません。", file=sys.stderr)
        return 1

    print(f"{len(traces)} 個のファイルを解析中...", file=sys.stderr)

    all_metrics = [analyze_trace(tp) for tp in traces]
    penalties = compute_straggler_penalty(all_metrics)

    output = {
        "analysis_timestamp": __import__("datetime").datetime.now().isoformat(),
        "traces_analyzed": len(all_metrics),
        "per_device": [],
        "straggler_penalty_ratio": penalties,
    }

    for m in all_metrics:
        d = asdict(m)
        d["straggler_penalty_ratio"] = penalties.get(m.device_ip, 0.0)
        output["per_device"].append(d)

    result_json = json.dumps(output, indent=2, ensure_ascii=False)

    if args.out:
        Path(args.out).write_text(result_json + "\n", encoding="utf-8")
        print(f"結果を保存しました: {args.out}", file=sys.stderr)
    else:
        print(result_json)

    # CUIへのサマリー出力
    print(f"\n{'='*70}", file=sys.stderr)
    for m in all_metrics:
        p = penalties.get(m.device_ip, 0)
        print(f"[{m.device_ip or m.trace_path.split('/')[-1]}]", file=sys.stderr)
        print(f"  Interrupts: {m.interrupt_count} | LMK Kills: {m.lmk_kill_count} | Binder: {m.binder_transaction_count}", file=sys.stderr)
        print(f"  Other CPU: {m.other_app_cpu_time_ms:.0f}ms | Straggler Penalty: {p:.3f}", file=sys.stderr)
    print(f"{'='*70}", file=sys.stderr)

    return 0

if __name__ == "__main__":
    sys.exit(main())