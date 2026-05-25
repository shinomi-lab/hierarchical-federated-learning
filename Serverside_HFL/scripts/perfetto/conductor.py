#!/usr/bin/env python3
"""
HFL Trace Conductor — 全台 atrace 一括制御スクリプト

端末構成:
  - atrace 16K (.11, .13, .14): バッファ 16384 KB
  - atrace  8K (.12):           バッファ  8192 KB (タイムアウト防止)

使い方:
  python3 scripts/perfetto/conductor.py start      # 全端末のトレース開始
  python3 scripts/perfetto/conductor.py stop       # 停止 & トレース回収
  python3 scripts/perfetto/conductor.py status     # 各端末の状態確認
  python3 scripts/perfetto/conductor.py connect    # 接続のみ（デバッグ用）
  python3 scripts/perfetto/conductor.py verify     # 1台で全サイクル検証

サーバ連動:
  conductor.start_all() / conductor.stop_all() を import して使える。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── 端末定義 ──────────────────────────────────────────────────────────────
# { IP: buffer_kb }
# 環境変数 PERFETTO_DEVICES で上書き可能。形式: "ip1:buf1,ip2:buf2,..."
# 例: PERFETTO_DEVICES="192.168.11.11:16384,192.168.11.12:8192"
def _parse_device_config() -> Dict[str, int]:
    env = os.environ.get("PERFETTO_DEVICES")
    if env:
        cfg = {}
        for entry in env.split(","):
            entry = entry.strip()
            if ":" in entry:
                ip, buf = entry.rsplit(":", 1)
                cfg[ip.strip()] = int(buf.strip())
        return cfg
    return {
        "192.168.11.11": 16384,
        "192.168.11.13": 16384,
        "192.168.11.14": 16384,
        "192.168.11.12": 8192,   # Android 9: タイムアウト防止
    }

DEVICE_CONFIG: Dict[str, int] = _parse_device_config()

PACKAGE = "com.example.hfl_experiment"
ATRACE_CATEGORIES = "sched idle am wm"

MAX_RETRY = 8
RETRY_DELAY = 3.0
ADB_TIMEOUT = 60        # 汎用 adb コマンド
CONNECT_TIMEOUT = 30     # adb connect
PULL_TIMEOUT = 120       # トレース回収

HERE = Path(__file__).resolve().parent
OUTPUT_ROOT = HERE / "traces"

# ── 色出力 ────────────────────────────────────────────────────────────────
def _red(s: str) -> str:    return f"\033[91m{s}\033[0m"
def _green(s: str) -> str:  return f"\033[92m{s}\033[0m"
def _yellow(s: str) -> str: return f"\033[93m{s}\033[0m"
def _bold(s: str) -> str:   return f"\033[1m{s}\033[0m"

# ── adb パス解決 ──────────────────────────────────────────────────────────
_SDK_PT = Path.home() / "Library" / "Android" / "sdk" / "platform-tools"
if _SDK_PT.exists() and str(_SDK_PT) not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{_SDK_PT}{os.pathsep}{os.environ.get('PATH', '')}"


def get_devices() -> Dict[str, int]:
    """環境変数での上書きをサポート。"""
    env = os.environ.get("HFL_ATRACE_DEVICES", "").strip()
    if env:
        return {ip.strip(): 16384 for ip in env.split(",") if ip.strip()}
    return dict(DEVICE_CONFIG)


# ── ADB ヘルパー ──────────────────────────────────────────────────────────

def adb(device: str, *args, timeout: int = ADB_TIMEOUT) -> Tuple[int, str]:
    cmd = ["adb", "-s", f"{device}:5555", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except FileNotFoundError:
        return -1, "adb not found in PATH"


def adb_shell(device: str, shell_cmd: str, timeout: int = ADB_TIMEOUT) -> Tuple[int, str]:
    return adb(device, "shell", shell_cmd, timeout=timeout)


# ── 接続 ─────────────────────────────────────────────────────────────────

def connect_device(ip: str) -> bool:
    for attempt in range(1, MAX_RETRY + 1):
        rc, out = adb(ip, "connect", f"{ip}:5555", timeout=CONNECT_TIMEOUT)
        if "connected" in out.lower():
            print(f"  [{ip}] {_green('connected')} (attempt {attempt})")
            return True
        print(f"  [{ip}] attempt {attempt}/{MAX_RETRY}: {out}")
        time.sleep(RETRY_DELAY)
    print(_red(f"  [{ip}] FAILED after {MAX_RETRY} attempts — skipping"), file=sys.stderr)
    return False


def connect_all(devices: List[str]) -> List[str]:
    if not devices:
        return []
    connected: List[str] = []
    with ThreadPoolExecutor(max_workers=len(devices)) as ex:
        futures = {ex.submit(connect_device, ip): ip for ip in devices}
        for f in as_completed(futures):
            if f.result():
                connected.append(futures[f])
    return connected


# ── クリーンアップ ────────────────────────────────────────────────────────

def cleanup_device(ip: str) -> None:
    """start前に前回のゾンビトレースを掃除。"""
    adb_shell(ip, "atrace --async_stop > /dev/null 2>&1", timeout=15)
    adb_shell(ip, "pkill -9 perfetto 2>/dev/null", timeout=10)
    print(f"  [{ip}] cleanup done")


def cleanup_all(connected: List[str]) -> None:
    print("\n2. Cleanup (zombie traces)...")
    with ThreadPoolExecutor(max_workers=len(connected)) as ex:
        list(ex.map(cleanup_device, connected))


# ── atrace 制御 ──────────────────────────────────────────────────────────

def start_atrace(ip: str, buffer_kb: int) -> bool:
    cmd = f"atrace --async_start -b {buffer_kb} {ATRACE_CATEGORIES}"
    rc, out = adb_shell(ip, cmd, timeout=30)
    if rc == 0:
        print(f"  [{ip}] {_green('atrace started')} (buf={buffer_kb}KB)")
        return True
    print(_red(f"  [{ip}] atrace start FAILED: {out}"), file=sys.stderr)
    return False


def stop_atrace(ip: str) -> bool:
    adb_shell(ip, "atrace --async_stop > /dev/null 2>&1", timeout=15)
    print(f"  [{ip}] atrace stopped")
    return True


def pull_atrace(ip: str, out_dir: Path, timestamp: str) -> Optional[Path]:
    """タイムスタンプ付きファイル名で回収。例: trace_12_20260506_1600.ctrace

    手順: --async_dump -z でバッファ内容を取得 → --async_stop で後片付け
    注意: --async_stop はバッファを破棄するだけでダンプしない
    """
    suffix = ip.split(".")[-1]
    dest = out_dir / f"trace_{suffix}_{timestamp}.ctrace"
    # 1. バッファの中身をダンプ（-z で圧縮バイナリ出力）
    cmd = ["adb", "-s", f"{ip}:5555", "shell", "atrace", "--async_dump", "-z"]
    try:
        with open(dest, "wb") as f:
            r = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, timeout=PULL_TIMEOUT)
        # 2. トレース停止 & バッファ解放
        adb_shell(ip, "atrace --async_stop > /dev/null 2>&1", timeout=15)
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 100:
            size_kb = dest.stat().st_size / 1024
            print(f"  [{ip}] {_green('pulled')} → {dest.name} ({size_kb:.1f} KB)")
            return dest
        print(_red(f"  [{ip}] pull failed or empty ({dest.stat().st_size} bytes): {r.stderr.decode(errors='replace')}"), file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(_red(f"  [{ip}] pull TIMEOUT"), file=sys.stderr)
    except FileNotFoundError:
        print(_red(f"  [{ip}] adb not found"), file=sys.stderr)
    return None


# ── 同期ブロードキャスト ──────────────────────────────────────────────────

def send_sync_broadcast(devices: List[str]) -> None:
    print("\n4. Sending SYNC_START broadcast...")
    with ThreadPoolExecutor(max_workers=len(devices)) as ex:
        list(ex.map(
            lambda ip: adb_shell(ip, f"am broadcast -a {PACKAGE}.SYNC_START"),
            devices,
        ))
    print(f"  Broadcast sent to {len(devices)} devices")


def dump_analyze_script(out_dir: Path) -> None:
    """自動解析スクリプトをトレースフォルダ内に同梱する"""
    script_content = r'''#!/usr/bin/env python3
import os
import re
import matplotlib.pyplot as plt
from pathlib import Path
from collections import Counter

# --- 設定：解析対象アプリ名（完全一致推奨） ---
TARGET_APP = ".hfl_experiment"
OUTPUT_DIR = "hfl_absolute_reports"

class HFLAbsoluteAnalyzer:
    def __init__(self, trace_path):
        self.trace_path = Path(trace_path)
        self.device_id = self.trace_path.stem
        
        # 統計データ
        self.all_events = Counter()
        self.cpu_thieves = Counter()
        self.irq_events = Counter()
        
        # 時系列データ
        self.mem_timestamps = []
        self.mem_usage_mb = []
        self.latency_timestamps = []
        self.latency_ms = []
        
        # カウンター＆状態管理
        self.binder_count = 0
        self.total_interrupts = 0
        self.wakeup_time = None
        self.total_latency_sec = 0.0

    def parse(self):
        print(f"ディープスキャン中: {self.trace_path.name}...")
        # 正規表現: timestamp: event_name: details
        event_pattern = re.compile(r"(\d+\.\d+):\s+([a-zA-Z0-9_]+):")

        with open(self.trace_path, "r", errors="ignore") as f:
            for line in f:
                match = event_pattern.search(line)
                if not match: continue

                timestamp, event_name = float(match.group(1)), match.group(2)
                self.all_events[event_name] += 1

                # 1. Binder通信の検知
                if "binder" in event_name.lower() or "binder" in line.lower():
                    self.binder_count += 1

                # 2. メモリ推移 (rss_stat)
                if event_name == "rss_stat":
                    size_match = re.search(r"size=(\d+)", line)
                    if size_match:
                        self.mem_timestamps.append(timestamp)
                        self.mem_usage_mb.append(int(size_match.group(1)) / 1024 / 1024)

                # 3. ハードウェア割り込み・裏側処理 (irq / softirq)
                if "irq_handler_entry" in event_name or "softirq_entry" in event_name:
                    irq_match = re.search(r"name=([^ ]+)", line) or re.search(r"vec=([^ ]+)", line)
                    if irq_match:
                        self.irq_events[irq_match.group(1)] += 1

                # 4. Latency計算：アプリが「起きる」時刻を記録
                if event_name in ["sched_waking", "sched_wakeup"] and TARGET_APP in line:
                    self.wakeup_time = timestamp

                # 5. CPUコンテキストスイッチ解析
                if event_name == "sched_switch":
                    # アプリが「実行開始」した瞬間（Latencyの確定）
                    if f"next_comm={TARGET_APP}" in line and self.wakeup_time:
                        latency = timestamp - self.wakeup_time
                        if latency > 0 and latency < 1.0: # 異常値(1秒以上)を除外
                            self.total_latency_sec += latency
                            self.latency_timestamps.append(timestamp)
                            self.latency_ms.append(latency * 1000)
                        self.wakeup_time = None # リセット

                    # アプリがCPUを「奪われた/手放した」瞬間
                    if f"prev_comm={TARGET_APP}" in line:
                        self.total_interrupts += 1
                        thief_match = re.search(r"next_comm=([^ ]+)", line)
                        if thief_match:
                            self.cpu_thieves[thief_match.group(1)] += 1

    def plot_report(self):
        # グラフを4段構成で出力
        fig, axes = plt.subplots(4, 1, figsize=(12, 18))
        plt.subplots_adjust(hspace=0.5)

        # A. システム全体のイベントTOP10
        top_events = self.all_events.most_common(10)
        axes[0].bar([e[0] for e in top_events], [e[1] for e in top_events], color='gray')
        axes[0].set_title(f"Top 10 System Events ({self.device_id})")
        axes[0].set_ylabel("Count")

        # B. CPU強奪ランキング (swapper等)
        if self.cpu_thieves:
            top_thieves = self.cpu_thieves.most_common(10)
            axes[1].barh([t[0] for t in top_thieves], [t[1] for t in top_thieves], color='salmon')
            axes[1].set_title(f"CPU Preemption Causes (Total Switches: {self.total_interrupts})")
            axes[1].invert_yaxis()

        # C. メモリ推移とハードウェア割り込み
        if self.mem_timestamps:
            start_t = self.mem_timestamps[0]
            rel_ts = [t - start_t for t in self.mem_timestamps]
            axes[2].plot(rel_ts, self.mem_usage_mb, color='blue', linewidth=1.5, label='Memory (MB)')
            axes[2].set_title("Memory Usage Trend")
            axes[2].set_ylabel("MB")
            axes[2].grid(True)
        else:
            axes[2].text(0.5, 0.5, "No Memory Data", ha='center')

        # D. Runnable Latency (実行待ち時間) の推移
        if self.latency_timestamps:
            start_t = self.latency_timestamps[0]
            rel_ts = [t - start_t for t in self.latency_timestamps]
            axes[3].scatter(rel_ts, self.latency_ms, color='purple', alpha=0.5, s=10)
            axes[3].set_title(f"Runnable Latency Spikes (Total Loss: {self.total_latency_sec:.2f} sec)")
            axes[3].set_ylabel("Wait Time (ms)")
            axes[3].set_xlabel("Time (sec)")
            axes[3].grid(True)
        else:
            axes[3].text(0.5, 0.5, "No Latency Data", ha='center')

        save_path = Path(OUTPUT_DIR) / f"{self.device_id}_absolute.png"
        plt.savefig(save_path)
        plt.close()

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    # trace_*.txt ではなく、conductorが生成する .ctrace を読み込むように修正
    files = sorted(Path('.').glob("trace_*.ctrace"))
    analyzers = []

    print(f"\n{'='*105}\n HFL Absolute Trace Analysis System (with Latency Profiling) \n{'='*105}")
    for f in files:
        az = HFLAbsoluteAnalyzer(f)
        az.parse()
        az.plot_report()
        analyzers.append(az)

    # 修士論文用：究極の比較サマリーテーブル
    print("\n" + "="*105)
    print(f"{'Device ID':<25} | {'Wait Count':<12} | {'Binder':<10} | {'IRQ (HW)':<10} | {'Total Time Lost (sec)'}")
    print("-" * 105)
    for a in analyzers:
        swapper_val = sum(v for k, v in a.cpu_thieves.items() if "swapper" in k)
        irq_total = sum(a.irq_events.values())
        print(f"{a.device_id:<25} | {swapper_val:<12,} | {a.binder_count:<10,} | {irq_total:<10,} | {a.total_latency_sec:>18.3f} sec")
    print("="*105)
    print(f"\n全解析結果と詳細グラフを {OUTPUT_DIR} フォルダに保存しました。")

if __name__ == "__main__":
    main()
'''
    script_path = out_dir / "analyze_simple.py"
    script_path.write_text(script_content, encoding="utf-8")
    script_path.chmod(0o755)

# ── 統合コマンド ──────────────────────────────────────────────────────────

def start_all() -> List[str]:
    """
    全端末のトレース開始。start.py から呼ぶための API。
    Returns: started device IPs
    """
    devices = get_devices()
    all_ips = list(devices.keys())

    print(f"\n{'='*50}")
    print(_bold("  HFL Trace Start"))
    print(f"{'='*50}")
    for ip, buf in devices.items():
        print(f"  {ip}  buf={buf}KB")

    # 1. Connect
    print("\n1. Connecting all devices...")
    connected = connect_all(all_ips)
    if not connected:
        print(_red("ERROR: No devices connected"), file=sys.stderr)
        return []

    failed = set(all_ips) - set(connected)
    if failed:
        print(_red(f"\n  WARNING: {len(failed)} device(s) unreachable: {list(failed)}"))
        print("  Continuing with connected devices...\n")

    # 2. Cleanup
    cleanup_all(connected)

    # 3. Start atrace (parallel)
    print("\n3. Starting atrace...")
    started: List[str] = []
    with ThreadPoolExecutor(max_workers=len(connected)) as ex:
        futures = {
            ex.submit(start_atrace, ip, devices[ip]): ip
            for ip in connected
        }
        for f in as_completed(futures):
            ip = futures[f]
            if f.result():
                started.append(ip)

    # 4. Sync broadcast
    if started:
        send_sync_broadcast(started)

    # Summary
    print(f"\n{'='*50}")
    if len(started) == len(all_ips):
        print(_green(_bold(f"  Experiment Ready — {len(started)}/{len(all_ips)} devices tracing")))
    elif started:
        print(_yellow(f"  Partial start — {len(started)}/{len(all_ips)} devices tracing"))
    else:
        print(_red("  FAILED — no devices started"))
    print(f"{'='*50}\n")

    return started


def stop_all(run_id: Optional[str] = None) -> Path:
    """
    全端末のトレース停止 & 回収。start.py から呼ぶための API。
    Returns: 出力ディレクトリ
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    if not run_id:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    devices = get_devices()
    all_ips = list(devices.keys())

    print(f"\n{'='*50}")
    print(_bold("  HFL Trace Stop & Collect"))
    print(f"  Output: {out_dir}")
    print(f"{'='*50}")

    # 1. Reconnect
    print("\n1. Reconnecting...")
    connected = connect_all(all_ips)
    if not connected:
        print(_red("ERROR: No devices reachable"), file=sys.stderr)
        return out_dir

    failed = set(all_ips) - set(connected)
    if failed:
        print(_red(f"\n  WARNING: {len(failed)} device(s) unreachable: {list(failed)}"))

    # 2. Pull (sequential to avoid adb contention)
    print("\n2. Pulling trace files...")
    collected: List[Path] = []
    for ip in connected:
        p = pull_atrace(ip, out_dir, timestamp)
        if p:
            collected.append(p)

    # 4. Manifest
    manifest = {
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(),
        "devices": {ip: {"buffer_kb": devices.get(ip, 0), "collected": False} for ip in all_ips},
        "collected_files": [],
        "total_size_bytes": 0,
    }
    for p in collected:
        # extract IP suffix from filename: trace_12_... -> match against known IPs
        suffix = p.name.split("_")[1]
        # DEVICE_CONFIG のIPからサフィックスが一致するものを探す
        full_ip = next((ip for ip in all_ips if ip.endswith(f".{suffix}")), f"192.168.11.{suffix}")
        if full_ip in manifest["devices"]:
            manifest["devices"][full_ip]["collected"] = True
        manifest["collected_files"].append(p.name)
    manifest["total_size_bytes"] = sum(p.stat().st_size for p in collected)

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # 5. Dump analysis script
    dump_analyze_script(out_dir)

    # Summary
    print(f"\n{'='*50}")
    if len(collected) == len(connected):
        print(_green(_bold(f"  {len(collected)}/{len(all_ips)} traces collected")))
    elif collected:
        print(_yellow(f"  {len(collected)}/{len(all_ips)} traces collected (partial)"))
    else:
        print(_red("  No traces collected"))
    print(f"  Output:   {out_dir}")
    print(f"  Manifest: {manifest_path}")
    for p in collected:
        print(f"    {p.name}  ({p.stat().st_size / 1024:.1f} KB)")
    print(f"{'='*50}\n")

    return out_dir


# ── CLI コマンド ──────────────────────────────────────────────────────────

def cmd_connect() -> int:
    devices = get_devices()
    all_ips = list(devices.keys())
    print(f"Connecting to {len(all_ips)} devices...")
    connected = connect_all(all_ips)
    print(f"\n{_bold('Connected')}: {len(connected)}/{len(all_ips)}")
    for ip in connected:
        print(f"  {_green(ip)}  buf={devices[ip]}KB")
    for ip in set(all_ips) - set(connected):
        print(f"  {_red(ip)}  FAILED")
    return 0 if len(connected) == len(all_ips) else 1


def cmd_start() -> int:
    started = start_all()
    return 0 if started else 1


def cmd_stop() -> int:
    stop_all()
    return 0


def cmd_status() -> int:
    devices = get_devices()
    all_ips = list(devices.keys())
    print(_bold("=== Device Status ===\n"))
    connected = connect_all(all_ips)
    for ip in all_ips:
        if ip in connected:
            rc, out = adb_shell(ip, "getprop ro.build.version.release", timeout=10)
            android_ver = out.strip() if rc == 0 else "?"
            print(f"  {_green(ip)}  buf={devices[ip]}KB  Android {android_ver}")
        else:
            print(f"  {_red(ip)}  DISCONNECTED")
    return 0


def cmd_verify() -> int:
    """1台で 接続→クリーンアップ→開始→5秒待機→停止→回収 の全サイクルを検証。"""
    devices = get_devices()
    first_ip = list(devices.keys())[0]
    buf = devices[first_ip]

    print(_bold(f"=== Verify: {first_ip} (buf={buf}KB) ===\n"))

    print("[1] Connect...")
    if not connect_device(first_ip):
        return 1

    print("[2] Cleanup...")
    cleanup_device(first_ip)

    print("[3] Start atrace...")
    if not start_atrace(first_ip, buf):
        return 1

    print("[4] Wait 5s...")
    time.sleep(5)

    print("[5] Stop...")
    stop_atrace(first_ip)

    print("[6] Pull...")
    out_dir = OUTPUT_ROOT / "verify_test"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    trace = pull_atrace(first_ip, out_dir, ts)

    if trace and trace.exists() and trace.stat().st_size > 100:
        print(f"\n{_green('PASS')}: {trace.name} ({trace.stat().st_size} bytes)")
        return 0
    print(f"\n{_red('FAIL')}: trace missing or empty")
    return 1


# ── エントリポイント ──────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="HFL Trace Conductor (all-atrace)")
    ap.add_argument("command", choices=["start", "stop", "status", "connect", "verify"],
                    help="Action to perform")
    args = ap.parse_args()

    dispatch = {
        "connect": cmd_connect,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "verify": cmd_verify,
    }
    return dispatch[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
