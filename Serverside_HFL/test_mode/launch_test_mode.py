#!/usr/bin/env python3
"""
launch_test_mode.py
===================
PC ローカル統合テストモード一括起動スクリプト。

起動するもの:
  - Central server (localhost:8000)
  - Edge server 1 (localhost:8001, EDGE_SERVER_ID=edge-server-01)
  - Edge server 2 (localhost:8002, EDGE_SERVER_ID=edge-server-02)
  - Dummy terminal × 4 (clients, no port — outbound HTTP only)

挙動:
  すべて subprocess で立ち上げ、stdout/stderr を test_mode/logs/ に集める。
  Ctrl-C か --duration で全プロセス cleanup。
  終了時に edge log から aggregation_started / completed をカウントして判定。

使い方:
  python3 test_mode/launch_test_mode.py --hfl-mode async --cycles 5 --duration 90
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VENV_PY = ROOT / ".venv" / "bin" / "python"
LOG_DIR = HERE / "logs"


CENTRAL_PORT = 8000
EDGE_PORT_1 = 8001
EDGE_PORT_2 = 8002


def _ensure_log_dir():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 既存ログをローテート
    archive = LOG_DIR / "archive"
    archive.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    for f in LOG_DIR.glob("*.log"):
        shutil.move(str(f), str(archive / f"{ts}_{f.name}"))


def _spawn(
    name: str,
    cmd: list[str],
    env_extra: dict,
    log_path: Path,
) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(env_extra)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(log_path, "w", buffering=1)  # line-buffered
    p = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=f,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return p


def _wait_http(url: str, timeout: float = 30.0) -> bool:
    """URL が 200 を返すまで待つ。"""
    import httpx
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hfl-mode", default="async", choices=["sync", "async"])
    p.add_argument("--cycles", type=int, default=5, help="端末あたりサイクル数")
    p.add_argument("--rounds", type=int, default=1, help="繰り返し global round 数")
    p.add_argument("--n-terminals", type=int, default=4)
    p.add_argument("--duration", type=int, default=120,
                   help="最大実行時間（秒）。これを超えたら強制終了")
    p.add_argument("--k-edge", type=int, default=2)
    p.add_argument("--k-central", type=int, default=1)
    p.add_argument("--max-staleness", type=int, default=5)
    p.add_argument("--start-clients-after-s", type=float, default=5.0,
                   help="サーバ起動後、何秒待ってからクライアントを起動するか")
    p.add_argument("--no-clients", action="store_true",
                   help="クライアントを起動せず、サーバだけ立ち上げる")
    # --- TP/RTT スタブ (端末に伝播) ---
    p.add_argument("--metric-mode", default="noisy",
                   choices=["fixed", "noisy", "network"],
                   help="dummy_terminal の TP/RTT 生成モード")
    p.add_argument("--ap-profile", default="HFL_A24",
                   choices=["HFL_A24", "HFL_B"])
    args = p.parse_args()

    if not VENV_PY.exists():
        print(f"[エラー] venv Python がありません: {VENV_PY}", file=sys.stderr)
        print("先に `bash setup_venv.sh` を実行してください。", file=sys.stderr)
        sys.exit(1)

    _ensure_log_dir()

    # 共通環境変数 (async モードに必要)
    common_env = {
        "HFL_MODE":             args.hfl_mode,
        "K_EDGE":               str(args.k_edge),
        "K_CENTRAL":            str(args.k_central),
        "MAX_STALENESS_ROUNDS": str(args.max_staleness),
        "EDGE_DISABLE_RATE_LIMITS": "1",
        "EDGE_LOCAL_FORCE_SINGLE": "0",
        # localhost プロファイル: 全部 127.0.0.1
        "CENTRAL_SERVER_URL":   f"http://127.0.0.1:{CENTRAL_PORT}",
        "EDGE_URL":             f"http://127.0.0.1:{EDGE_PORT_1}",
        "PYTHONUNBUFFERED":     "1",
    }

    procs: list[tuple[str, subprocess.Popen]] = []

    print("=" * 60)
    print(" PC ローカルテストモード起動")
    print(f"   hfl_mode={args.hfl_mode}  K_EDGE={args.k_edge}  K_CENTRAL={args.k_central}")
    print(f"   terminals={args.n_terminals}  cycles={args.cycles}  rounds={args.rounds}")
    print(f"   duration={args.duration}s")
    print("=" * 60)

    def cleanup():
        print("\n=== クリーンアップ ===")
        for name, p in procs:
            if p.poll() is None:
                print(f"  terminate {name} (pid={p.pid})")
                try:
                    p.terminate()
                except Exception:
                    pass
        time.sleep(2)
        for name, p in procs:
            if p.poll() is None:
                print(f"  kill {name} (pid={p.pid})")
                try:
                    p.kill()
                except Exception:
                    pass

    def sig_handler(signum, frame):
        print(f"\n[シグナル受信 {signum}] 終了処理")
        sig_stats = _fetch_live_upload_stats()
        cleanup()
        _report(args, sig_stats)
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        # ── Central ─────────────────────────────────
        print(f"\n[1/3] Central server を起動 (port {CENTRAL_PORT})")
        central_env = {**common_env}
        central_p = _spawn(
            "central",
            [str(VENV_PY), "-m", "uvicorn", "central_server.main:app",
             "--host", "127.0.0.1", "--port", str(CENTRAL_PORT)],
            central_env,
            LOG_DIR / "central.log",
        )
        procs.append(("central", central_p))

        if not _wait_http(f"http://127.0.0.1:{CENTRAL_PORT}/docs", timeout=20):
            print("  ❌ Central server が起動しませんでした", file=sys.stderr)
            cleanup()
            sys.exit(2)
        print(f"  ✅ Central ready  log={LOG_DIR / 'central.log'}")

        # ── Edge × 2 ─────────────────────────────────
        for i, port in enumerate([EDGE_PORT_1, EDGE_PORT_2], start=1):
            edge_id = f"edge-server-{i:02d}"
            print(f"\n[2/3] Edge {edge_id} を起動 (port {port})")
            edge_env = {
                **common_env,
                "EDGE_SERVER_ID":   edge_id,
                "EDGE_SERVER_PORT": str(port),
                "EDGE_URL":         f"http://127.0.0.1:{port}",
            }
            edge_p = _spawn(
                edge_id,
                [str(VENV_PY), "-m", "uvicorn", "edge_server.main:app",
                 "--host", "127.0.0.1", "--port", str(port)],
                edge_env,
                LOG_DIR / f"{edge_id}.log",
            )
            procs.append((edge_id, edge_p))

            if not _wait_http(f"http://127.0.0.1:{port}/api/v1/meta", timeout=20):
                print(f"  ❌ {edge_id} が起動しませんでした", file=sys.stderr)
                cleanup()
                sys.exit(2)
            print(f"  ✅ {edge_id} ready  log={LOG_DIR / f'{edge_id}.log'}")

        if args.no_clients:
            print("\n[3/3] --no-clients 指定なのでクライアントは起動しません")
            print("\n手動で curl やテストを叩く場合はここで動作確認してください。")
            print("Ctrl-C で終了。")
            while True:
                time.sleep(5)

        # ── Terminals ─────────────────────────────────
        print(f"\n  サーバ安定待機 {args.start_clients_after_s}s...")
        time.sleep(args.start_clients_after_s)

        print(f"\n[3/3] Terminal × {args.n_terminals} を起動")
        # 偶数台は edge-01、奇数台は edge-02 に振る（実機トポロジー想定）
        edge_urls = [f"http://127.0.0.1:{EDGE_PORT_1}", f"http://127.0.0.1:{EDGE_PORT_2}"]
        for tid_idx in range(1, args.n_terminals + 1):
            terminal_id = f"terminal-{tid_idx:02d}"
            edge_url = edge_urls[(tid_idx - 1) % len(edge_urls)]
            t_env = {
                **common_env,
                "TERMINAL_ID": terminal_id,
            }
            t_p = _spawn(
                terminal_id,
                [str(VENV_PY), str(HERE / "dummy_terminal.py"),
                 "--terminal-id", terminal_id,
                 "--edge-url", edge_url,
                 "--hfl-mode", args.hfl_mode,
                 "--cycles", str(args.cycles),
                 "--rounds", str(args.rounds),
                 "--start-delay-s", str(0.2 * (tid_idx - 1)),
                 "--metric-mode", args.metric_mode,
                 "--ap-profile", args.ap_profile,
                 "--n-on-ap", str(args.n_terminals // 2),   # 2エッジに均等分散
                 ],
                t_env,
                LOG_DIR / f"{terminal_id}.log",
            )
            procs.append((terminal_id, t_p))
            print(f"  ✅ {terminal_id} → {edge_url}")

        # ── 監視ループ ─────────────────────────────────
        print(f"\n=== 実行中 (最大 {args.duration}s) ===")
        deadline = time.monotonic() + args.duration
        terminals_done = False
        while time.monotonic() < deadline:
            time.sleep(1)
            # 全 terminal が終了したらサーバも畳む
            term_running = sum(
                1 for n, p in procs if n.startswith("terminal-") and p.poll() is None
            )
            if term_running == 0 and not terminals_done:
                print(f"\n  全 terminal 終了。サーバを 3 秒猶予して停止します。")
                terminals_done = True
                deadline = time.monotonic() + 3
                continue
            # サーバが落ちたら異常検知
            for n, p in procs:
                if not n.startswith("terminal-") and p.poll() is not None:
                    print(f"\n  ⚠️ {n} が予期せず終了 (exit={p.returncode}) — log={LOG_DIR / f'{n}.log'}")

    finally:
        live_stats = _fetch_live_upload_stats()   # サーバが生きている間に取得
        cleanup()
        _report(args, live_stats)


def _fetch_live_upload_stats() -> dict:
    """サーバ起動中にアップロード統計を取得する。"""
    result = {}
    try:
        import httpx
        for port, label in [(EDGE_PORT_1, "edge-server-01"), (EDGE_PORT_2, "edge-server-02")]:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/admin/upload_stats", timeout=3.0)
                if r.status_code == 200:
                    result[label] = r.json()
            except Exception:
                pass
        try:
            r = httpx.get(f"http://127.0.0.1:{CENTRAL_PORT}/upload_stats", timeout=3.0)
            if r.status_code == 200:
                result["central"] = r.json()
        except Exception:
            pass
    except ImportError:
        pass
    return result


def _report(args, live_stats: dict = None):
    """終了時にレポート出力"""
    print("\n" + "=" * 60)
    print(" 集約結果レポート")
    print("=" * 60)

    edge_logs = sorted(LOG_DIR.glob("edge-server-*.log"))
    for elog in edge_logs:
        started = 0
        completed = 0
        unbound = 0
        empty = 0
        try:
            with open(elog, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "aggregation_started" in line:
                        started += 1
                    if "aggregation_completed" in line:
                        completed += 1
                    if "UnboundLocalError" in line:
                        unbound += 1
                    if "aggregation_empty_bucket" in line:
                        empty += 1
        except Exception:
            continue
        verdict = "✅" if completed > 0 else "❌"
        print(f"  {verdict} {elog.name}:")
        print(f"      aggregation_started   = {started}")
        print(f"      aggregation_completed = {completed}")
        print(f"      UnboundLocalError     = {unbound}  {'⚠️' if unbound > 0 else ''}")
        print(f"      empty_bucket          = {empty}    {'⚠️' if empty > 0 else ''}")

    central_log = LOG_DIR / "central.log"
    if central_log.exists():
        with open(central_log, encoding="utf-8", errors="replace") as f:
            text = f.read()
        # async モード (central_server/main.py): async_aggregation_triggered + aggregation_completed
        # sync モード: aggregation_completed のみ
        # legacy (central_server.py): global_aggregated
        async_trig = text.count("async_aggregation_triggered")
        async_done = text.count("aggregation_completed")
        sync_done  = text.count("global_aggregated")
        edge_post = text.count("POST /edge_update HTTP")
        # 集約完了の合計が 1 以上なら成功 (どのモードでも aggregation_completed が出る)
        verdict_c = "✅" if (async_done + sync_done) > 0 else "❌"
        print(f"  {verdict_c} Central:")
        print(f"      POST /edge_update      = {edge_post}")
        print(f"      async_aggregation_triggered = {async_trig}")
        print(f"      aggregation_completed       = {async_done}")
        print(f"      global_aggregated (legacy)  = {sync_done}")

    # ── アップロード統計 + 検証 (Bug 3 修正) ────────────────────────
    print("\n--- アップロード統計 (非同期HFL: 誰が何回送ったか) ---")
    stats = live_stats or {}
    stats_failures: list[str] = []

    # ログから aggregation_completed カウントを取り出す（検証用）
    log_agg_completed: dict[str, int] = {}
    for elog in sorted(LOG_DIR.glob("edge-server-*.log")):
        cnt = 0
        try:
            with open(elog, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "aggregation_completed" in line:
                        cnt += 1
        except Exception:
            pass
        log_agg_completed[elog.stem] = cnt

    for edge_label in ["edge-server-01", "edge-server-02"]:
        d = stats.get(edge_label)
        if d:
            tuc = d.get("terminal_upload_counts", {})
            agg_count = d.get("edge_aggregation_count", 0)
            log_agg = log_agg_completed.get(edge_label, 0)

            print(f"  {edge_label} (round={d.get('round_current','?')}):")
            for tid, cnt in sorted(tuc.items()):
                ok = cnt >= 1
                mark = "✅" if ok else "❌"
                print(f"    {mark} {tid}: {cnt} 回{'  ⚠️ 未送信' if not ok else ''}")
                if not ok:
                    stats_failures.append(f"{edge_label}/{tid}: アップロード回数=0")
            print(f"    → edge集約送信 合計: {agg_count} 回  (ログ上 aggregation_completed={log_agg})")
            # 検証: edge_aggregation_count == ログの aggregation_completed
            if agg_count != log_agg:
                mark = "⚠️"
                stats_failures.append(
                    f"{edge_label}: edge_aggregation_count({agg_count}) ≠ log aggregation_completed({log_agg})"
                )
            else:
                mark = "✅"
            print(f"    → カウント一致確認: {mark}")
        else:
            print(f"  {edge_label}: 統計未取得（サーバ停止後の取得失敗）")

    d = stats.get("central")
    if d:
        pec = d.get("per_edge_recv_counts", {})
        c_agg = d.get("central_aggregation_count", 0)
        total_edge_uploads = sum(pec.values())
        print(f"  central:")
        for eid, cnt in sorted(pec.items()):
            print(f"    {eid}: {cnt} 回受信")
        print(f"    → central集約完了 合計: {c_agg} 回")
        # 検証: central 受信総数 == edge 集約送信総数
        edge_total_agg = sum(
            stats.get(e, {}).get("edge_aggregation_count", 0)
            for e in ["edge-server-01", "edge-server-02"]
        )
        if edge_total_agg > 0 and total_edge_uploads != edge_total_agg:
            stats_failures.append(
                f"central受信合計({total_edge_uploads}) ≠ edge集約送信合計({edge_total_agg})"
            )
    else:
        print("  central: 統計未取得")

    # 統計検証まとめ
    if stats_failures:
        print("\n  ⚠️ 統計検証 NG:")
        for f in stats_failures:
            print(f"    - {f}")
    elif stats:
        print("\n  ✅ 統計検証 OK (全端末送信済み, カウント一致)")

    print("\n  各ログ: ls test_mode/logs/")


if __name__ == "__main__":
    main()
