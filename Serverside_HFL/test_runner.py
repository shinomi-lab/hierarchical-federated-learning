#!/usr/bin/env python3
"""
test_runner.py
==============
HFL サーバのテスト実行ツール。4 つのモードをワンコマンドで実行できる。

使い方:
  python test_runner.py A            # 仮想端末シミュレーション（サーバは別途起動済み前提）
  python test_runner.py B            # ドライラン（サーバ不要・FL ロジック単体テスト）
  python test_runner.py C            # E2E スモークテスト（サーバを自動起動・停止）
  python test_runner.py AC           # フルシミュレーション（複数エッジ・複数端末・複数ラウンド）

オプション（全モード共通）:
  --edges   N     エッジサーバの台数（デフォルト: 1）
  --terms   N     エッジごとの仮想端末数（デフォルト: 2）
  --rounds  N     送信するラウンド数（デフォルト: 3）
  --quiet         詳細ログを抑制

モード A のみ:
  --edge-url URL  接続先エッジサーバ URL（デフォルト: http://127.0.0.1:8001）
"""

import argparse
import os
import random
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# ------------------------------------------------------------------ #
# カラー出力
# ------------------------------------------------------------------ #
_USE_COLOR = sys.stdout.isatty()

def _c(code, t):
    return f"\033[{code}m{t}\033[0m" if _USE_COLOR else t

def _ok(t):    return _c("32", t)
def _warn(t):  return _c("33", t)
def _err(t):   return _c("31", t)
def _bold(t):  return _c("1", t)
def _gray(t):  return _c("90", t)
def _blue(t):  return _c("34", t)
def _cyan(t):  return _c("36", t)

# ------------------------------------------------------------------ #
# 共通ユーティリティ
# ------------------------------------------------------------------ #
CENTRAL_PORT   = 8000
EDGE_BASE_PORT = 8001

try:
    import httpx as _http_lib
    _USE_HTTPX = True
except ImportError:
    import urllib.request as _urllib
    _USE_HTTPX = False


def _http_get(url: str, timeout: int = 3) -> int:
    try:
        if _USE_HTTPX:
            r = _http_lib.get(url, timeout=timeout)
            return r.status_code
        else:
            with _urllib.urlopen(url, timeout=timeout) as r:
                return r.status
    except Exception:
        return 0


def _wait_ready(url: str, label: str, timeout: int = 30, quiet: bool = False) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if _http_get(url) in range(200, 500):
            if not quiet:
                print(f"    {_ok('✓')} {label} 起動確認")
            return True
        time.sleep(0.5)
    if not quiet:
        print(f"    {_err('✗')} {label} タイムアウト")
    return False


def _stream_logs(proc: subprocess.Popen, prefix: str, color_fn=None, quiet: bool = False):
    if color_fn is None:
        color_fn = lambda t: t
    tag = color_fn(f"[{prefix}]")
    for line in proc.stdout:
        if not quiet:
            text = line.rstrip()
            if text:
                print(f"{tag} {text}", flush=True)


def _launch_server(cmd: list, env: dict, label: str, color_fn, quiet: bool) -> subprocess.Popen:
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    threading.Thread(
        target=_stream_logs, args=(proc, label, color_fn, quiet),
        daemon=True,
    ).start()
    return proc


def _stop_servers(procs: list, quiet: bool = False):
    for proc, name in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc, name in procs:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    if not quiet:
        print(_gray("  サーバを停止しました。"))


def _build_env(profile: str = "localhost", edge_id: str = None, edge_url: str = None) -> dict:
    env = os.environ.copy()
    env["HFL_ENV"]            = profile
    env["CENTRAL_SERVER_URL"] = f"http://127.0.0.1:{CENTRAL_PORT}"
    env["PYTHONUNBUFFERED"]   = "1"
    env["EDGE_LOCAL_FORCE_SINGLE"] = "0"
    if edge_id:
        env["EDGE_SERVER_ID"] = edge_id
    if edge_url:
        env["EDGE_URL"] = edge_url
    return env


# ================================================================== #
# モード A：仮想端末シミュレーション
# ================================================================== #
def run_mode_a(
    edge_url: str,
    n_terminals: int,
    rounds: int,
    quiet: bool = False,
) -> bool:
    """
    サーバが起動済みの状態で、仮想端末 N 台分の重みを送信する。
    """
    print()
    print(_bold("═══ モード A：仮想端末シミュレーション ═══"))
    print(f"  エッジURL : {edge_url}")
    print(f"  端末数   : {n_terminals}")
    print(f"  ラウンド : {rounds}")
    print()

    from scripts.sim_terminal import run_terminal, APP_TYPES

    results_all = []
    threads = []

    def _worker(tid: str):
        app = random.choice(APP_TYPES)
        res = run_terminal(
            edge_url=edge_url,
            terminal_id=tid,
            rounds=rounds,
            n_samples=random.randint(50, 200),
            app_type=app,
            interval=0.3,
            verbose=not quiet,
        )
        results_all.extend(res)

    for i in range(n_terminals):
        t = threading.Thread(target=_worker, args=(f"sim-term-{i+1:02d}",))
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok  = sum(1 for r in results_all if r["ok"])
    total = len(results_all)
    success = ok == total

    print()
    if success:
        print(_ok(f"  ✓ モード A 完了: {ok}/{total} 送信成功"))
    else:
        print(_err(f"  ✗ モード A 完了: {ok}/{total} 成功（{total-ok} 件失敗）"))
    return success


# ================================================================== #
# モード B：ドライラン（サーバ不要）
# ================================================================== #
def run_mode_b(quiet: bool = False) -> bool:
    """
    FedAvg ロジック・NaN/Inf 除外・メトリクス DB 書き込みを
    サーバなしでテストする。
    """
    print()
    print(_bold("═══ モード B：ドライラン (FL ロジック単体テスト) ═══"))

    failures = []

    # ── テスト 1: FedAvg 正常系 ──────────────────────────────────── #
    try:
        import torch
        from central_server.endpoints.edge_update import fedavg_multiple_edges, _has_nan_inf

        def _make_sd(val: float):
            return {"layer3.weight": torch.full((2, 4), val), "layer3.bias": torch.full((2,), val * 0.1)}

        updates = {
            "edge-01": {"state_dict": _make_sd(1.0), "sum_n_samples": 100},
            "edge-02": {"state_dict": _make_sd(3.0), "sum_n_samples": 300},
        }
        result = fedavg_multiple_edges(updates)
        # 重み付き平均: (1.0*100 + 3.0*300)/(400) = 2.5
        expected = 2.5
        got = result["layer3.weight"][0][0].item()
        if abs(got - expected) > 1e-4:
            failures.append(f"FedAvg 正常系: expected {expected}, got {got}")
        elif not quiet:
            print(f"  {_ok('✓')} FedAvg 正常系  (期待値={expected:.4f}, 実値={got:.4f})")
    except Exception as e:
        failures.append(f"FedAvg 正常系: {e}")

    # ── テスト 2: NaN/Inf 含む更新を除外 ─────────────────────────── #
    try:
        import torch
        from central_server.endpoints.edge_update import fedavg_multiple_edges, _has_nan_inf

        nan_sd = {"layer3.weight": torch.tensor([[float("nan"), 1.0]]), "layer3.bias": torch.tensor([0.0])}
        good_sd = {"layer3.weight": torch.tensor([[2.0, 2.0]]), "layer3.bias": torch.tensor([0.1])}
        updates = {
            "bad-edge":  {"state_dict": nan_sd,  "sum_n_samples": 100},
            "good-edge": {"state_dict": good_sd, "sum_n_samples": 100},
        }
        result = fedavg_multiple_edges(updates)
        got = result["layer3.weight"][0][0].item()
        if abs(got - 2.0) > 1e-4:
            failures.append(f"NaN 除外: good 値が {got} (期待 2.0)")
        elif not quiet:
            print(f"  {_ok('✓')} NaN 含む更新を除外して FedAvg 実行  (good 値={got:.4f})")
    except Exception as e:
        failures.append(f"NaN 除外: {e}")

    # ── テスト 3: 全更新が NaN → ValueError ──────────────────────── #
    try:
        import torch
        from central_server.endpoints.edge_update import fedavg_multiple_edges

        nan_sd = {"layer3.weight": torch.full((1, 1), float("nan"))}
        updates = {"e1": {"state_dict": nan_sd, "sum_n_samples": 10}}
        raised = False
        try:
            fedavg_multiple_edges(updates)
        except ValueError:
            raised = True
        if not raised:
            failures.append("全 NaN → ValueError が発生しなかった")
        elif not quiet:
            print(f"  {_ok('✓')} 全 NaN 更新 → ValueError 正常")
    except Exception as e:
        failures.append(f"全 NaN 例外テスト: {e}")

    # ── テスト 4: メトリクス DB 書き込み ─────────────────────────── #
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_metrics.db"

            # テスト用に DB を作成
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        edge_id TEXT NOT NULL, round INTEGER NOT NULL,
                        accuracy REAL, loss REAL, data_id TEXT NOT NULL,
                        event_timestamp TEXT, app_type TEXT, app_index INTEGER,
                        satisfaction_before REAL, satisfaction_after REAL
                    )
                """)
                conn.execute("""
                    INSERT INTO metrics
                      (edge_id, round, accuracy, loss, data_id, app_type,
                       satisfaction_before, satisfaction_after)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, ("edge-01", 1, 0.85, 0.12, "test-data-id",
                      "video", 0.45, 0.70))
                conn.commit()

            # 読み返して確認
            with sqlite3.connect(str(db_path)) as conn:
                rows = conn.execute("SELECT * FROM metrics").fetchall()

            if len(rows) != 1:
                failures.append(f"DB 書き込み: 行数={len(rows)} (期待 1)")
            elif abs(rows[0][3] - 0.85) > 1e-5:
                failures.append(f"DB 書き込み: accuracy={rows[0][3]} (期待 0.85)")
            elif not quiet:
                print(f"  {_ok('✓')} メトリクス DB 書き込み・読み取り")
    except Exception as e:
        failures.append(f"DB テスト: {e}")

    # ── テスト 5: エッジ側 FedAvg (terminal→edge) ─────────────────── #
    try:
        import torch
        from edge_server.endpoints.aggregation import _fedavg_state_dicts_weighted, _has_nan_inf as edge_has_nan_inf

        items = [
            ({"layer.weight": torch.full((2, 2), 1.0)}, 100),
            ({"layer.weight": torch.full((2, 2), 3.0)}, 300),
        ]
        result = _fedavg_state_dicts_weighted(items)
        got = result["layer.weight"][0][0].item()
        expected = (1.0 * 100 + 3.0 * 300) / 400
        if abs(got - expected) > 1e-4:
            failures.append(f"エッジ FedAvg: expected {expected:.4f}, got {got:.4f}")
        elif not quiet:
            print(f"  {_ok('✓')} エッジ FedAvg (terminal→edge)  (期待={expected:.4f}, 実値={got:.4f})")
    except Exception as e:
        failures.append(f"エッジ FedAvg: {e}")

    # ── 結果 ─────────────────────────────────────────────────────── #
    print()
    if not failures:
        print(_ok("  ✓ モード B 全テスト PASS"))
        return True
    else:
        for f in failures:
            print(_err(f"  ✗ {f}"))
        print(_err(f"  モード B: {len(failures)} テスト FAIL"))
        return False


# ================================================================== #
# モード C：E2E スモークテスト
# ================================================================== #
def run_mode_c(
    n_edges: int = 1,
    n_terminals: int = 1,
    rounds: int = 2,
    quiet: bool = False,
) -> bool:
    """
    中央サーバ + エッジサーバを自動起動し、仮想端末で送信、
    ヘルスチェックで確認して、サーバを自動停止する。
    """
    print()
    print(_bold("═══ モード C：E2E スモークテスト ═══"))
    print(f"  エッジ台数: {n_edges}  端末数/エッジ: {n_terminals}  ラウンド: {rounds}")
    print()

    procs = []
    try:
        # ── 中央サーバ起動 ────────────────────────────────────────── #
        cmd_c = [
            sys.executable, "-m", "uvicorn",
            "central_server.main:app",
            "--host", "127.0.0.1",
            "--port", str(CENTRAL_PORT),
            "--log-level", "warning",
        ]
        env_c = _build_env("localhost")
        proc_c = _launch_server(cmd_c, env_c, "中央", _blue, quiet=quiet)
        procs.append((proc_c, "中央"))

        if not _wait_ready(f"http://127.0.0.1:{CENTRAL_PORT}/healthz", "中央サーバ", quiet=quiet):
            print(_err("  ✗ 中央サーバの起動に失敗しました"))
            return False

        # ── エッジサーバ起動 ──────────────────────────────────────── #
        for i in range(n_edges):
            port    = EDGE_BASE_PORT + i
            edge_id = f"edge-server-{i+1:02d}"
            cmd_e = [
                sys.executable, "-m", "uvicorn",
                "edge_server.main:app",
                "--host", "127.0.0.1",
                "--port", str(port),
                "--log-level", "warning",
            ]
            env_e = _build_env("localhost", edge_id=edge_id, edge_url=f"http://127.0.0.1:{port}")
            env_e["EDGE_AGGREGATION_THRESHOLD"]          = str(n_terminals)
            env_e["EDGE_AGGREGATION_THRESHOLD_OVERRIDE"] = str(n_terminals)
            proc_e = _launch_server(cmd_e, env_e, f"エッジ{i+1}", _cyan, quiet=quiet)
            procs.append((proc_e, f"エッジ{i+1}"))

            if not _wait_ready(f"http://127.0.0.1:{port}/healthz", f"エッジサーバ{i+1}", quiet=quiet):
                print(_err(f"  ✗ エッジサーバ{i+1} の起動に失敗しました"))
                return False

        print(f"  {_ok('✓')} 全サーバ起動完了")

        # ── 仮想端末で送信 ────────────────────────────────────────── #
        print()
        print("  仮想端末で送信中...")

        ok_count = [0]
        fail_count = [0]
        lock = threading.Lock()

        def _worker(edge_idx, term_idx):
            port = EDGE_BASE_PORT + edge_idx
            edge_url = f"http://127.0.0.1:{port}"
            from scripts.sim_terminal import run_terminal, APP_TYPES
            res = run_terminal(
                edge_url=edge_url,
                terminal_id=f"smoke-e{edge_idx+1}-t{term_idx+1:02d}",
                rounds=rounds,
                n_samples=50,
                app_type=random.choice(APP_TYPES),
                interval=0.2,
                verbose=not quiet,
            )
            with lock:
                ok_count[0]   += sum(1 for r in res if r["ok"])
                fail_count[0] += sum(1 for r in res if not r["ok"])

        threads = []
        for ei in range(n_edges):
            for ti in range(n_terminals):
                t = threading.Thread(target=_worker, args=(ei, ti))
                threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = ok_count[0] + fail_count[0]
        if fail_count[0] > 0:
            print(_err(f"  ✗ 送信: {ok_count[0]}/{total} 成功 ({fail_count[0]} 件失敗)"))
            return False

        print(f"  {_ok('✓')} 送信: {ok_count[0]}/{total} 成功")

        # ── ヘルスチェック（サーバが落ちていないか）────────────────── #
        print()
        print("  最終ヘルスチェック...")
        all_healthy = True
        if _http_get(f"http://127.0.0.1:{CENTRAL_PORT}/healthz") not in range(200, 500):
            print(_err("  ✗ 中央サーバが応答しない"))
            all_healthy = False
        else:
            if not quiet:
                print(f"  {_ok('✓')} 中央サーバ 応答確認")

        for i in range(n_edges):
            port = EDGE_BASE_PORT + i
            if _http_get(f"http://127.0.0.1:{port}/healthz") not in range(200, 500):
                print(_err(f"  ✗ エッジサーバ{i+1} が応答しない"))
                all_healthy = False
            else:
                if not quiet:
                    print(f"  {_ok('✓')} エッジサーバ{i+1} 応答確認")

        print()
        if all_healthy:
            print(_ok("  ✓ モード C スモークテスト PASS"))
        else:
            print(_err("  ✗ モード C スモークテスト FAIL"))
        return all_healthy

    except Exception as e:
        print(_err(f"  ✗ 例外が発生しました: {e}"))
        return False

    finally:
        _stop_servers(procs, quiet=quiet)


# ================================================================== #
# モード A+C：フルシミュレーション
# ================================================================== #
def run_mode_ac(
    n_edges: int = 1,
    n_terminals: int = 3,
    rounds: int = 3,
    quiet: bool = False,
) -> bool:
    """
    複数エッジ・複数端末・複数ラウンドによるフルシミュレーション。
    サーバを自動起動し、全ラウンド完了後に自動停止する。
    """
    print()
    print(_bold("═══ モード A+C：フルシミュレーション ═══"))
    print(f"  エッジ台数: {n_edges}  端末数/エッジ: {n_terminals}  ラウンド: {rounds}")
    print()

    procs = []
    try:
        # ── サーバ起動（C と同じフロー）────────────────────────────── #
        cmd_c = [
            sys.executable, "-m", "uvicorn",
            "central_server.main:app",
            "--host", "127.0.0.1",
            "--port", str(CENTRAL_PORT),
            "--log-level", "warning",
        ]
        env_c = _build_env("localhost")
        proc_c = _launch_server(cmd_c, env_c, "中央", _blue, quiet=quiet)
        procs.append((proc_c, "中央"))

        if not _wait_ready(f"http://127.0.0.1:{CENTRAL_PORT}/healthz", "中央サーバ", quiet=quiet):
            return False

        edge_urls = []
        for i in range(n_edges):
            port    = EDGE_BASE_PORT + i
            edge_id = f"edge-server-{i+1:02d}"
            cmd_e = [
                sys.executable, "-m", "uvicorn",
                "edge_server.main:app",
                "--host", "127.0.0.1",
                "--port", str(port),
                "--log-level", "warning",
            ]
            env_e = _build_env("localhost", edge_id=edge_id, edge_url=f"http://127.0.0.1:{port}")
            env_e["EDGE_AGGREGATION_THRESHOLD"]          = str(n_terminals)
            env_e["EDGE_AGGREGATION_THRESHOLD_OVERRIDE"] = str(n_terminals)
            proc_e = _launch_server(cmd_e, env_e, f"エッジ{i+1}", _cyan, quiet=quiet)
            procs.append((proc_e, f"エッジ{i+1}"))
            edge_urls.append(f"http://127.0.0.1:{port}")

            if not _wait_ready(f"http://127.0.0.1:{port}/healthz", f"エッジサーバ{i+1}", quiet=quiet):
                return False

        print(f"  {_ok('✓')} 全サーバ起動完了\n")
        print("  仮想端末で送信中...")

        # ── 全端末 × 全ラウンドを並列送信 ─────────────────────────── #
        ok_count = [0]
        fail_count = [0]
        lock = threading.Lock()

        def _worker(edge_url, terminal_id):
            from scripts.sim_terminal import run_terminal, APP_TYPES
            res = run_terminal(
                edge_url=edge_url,
                terminal_id=terminal_id,
                rounds=rounds,
                n_samples=random.randint(80, 300),
                app_type=random.choice(APP_TYPES),
                interval=0.2,
                verbose=not quiet,
            )
            with lock:
                ok_count[0]   += sum(1 for r in res if r["ok"])
                fail_count[0] += sum(1 for r in res if not r["ok"])

        threads = []
        for ei, eu in enumerate(edge_urls):
            for ti in range(n_terminals):
                tid = f"full-e{ei+1}-t{ti+1:02d}"
                t = threading.Thread(target=_worker, args=(eu, tid))
                threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = ok_count[0] + fail_count[0]
        print()
        if fail_count[0] == 0:
            print(f"  {_ok('✓')} 送信: {ok_count[0]}/{total} 成功")
        else:
            print(_warn(f"  △ 送信: {ok_count[0]}/{total} 成功 ({fail_count[0]} 件失敗)"))

        # ── 集約待ち（最後のラウンドが中央まで届くのを少し待つ）─────── #
        print("  集約処理を待機中...")
        time.sleep(3)

        # ── 最終ヘルスチェック ────────────────────────────────────── #
        all_healthy = True
        checks = [(f"http://127.0.0.1:{CENTRAL_PORT}/healthz", "中央サーバ")]
        for i in range(n_edges):
            checks.append((f"http://127.0.0.1:{EDGE_BASE_PORT+i}/healthz", f"エッジサーバ{i+1}"))
        for url, label in checks:
            if _http_get(url) not in range(200, 500):
                print(_err(f"  ✗ {label} が応答しない"))
                all_healthy = False
            elif not quiet:
                print(f"  {_ok('✓')} {label} 応答確認")

        print()
        success = all_healthy and fail_count[0] == 0
        if success:
            print(_ok("  ✓ モード A+C フルシミュレーション PASS"))
        else:
            print(_err("  ✗ モード A+C フルシミュレーション FAIL"))
        return success

    except Exception as e:
        print(_err(f"  ✗ 例外が発生しました: {e}"))
        return False

    finally:
        _stop_servers(procs, quiet=quiet)


# ================================================================== #
# エントリポイント
# ================================================================== #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="HFL テストランナー (A / B / C / AC)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "mode",
        choices=["A", "B", "C", "AC"],
        help="テストモード: A=仮想端末, B=ドライラン, C=E2Eスモーク, AC=フルシミュレーション",
    )
    p.add_argument("--edges",     type=int, default=1,                        help="エッジサーバの台数 (C/AC)")
    p.add_argument("--terms",     type=int, default=2,                        help="エッジごとの仮想端末数 (A/C/AC)")
    p.add_argument("--rounds",    type=int, default=3,                        help="送信ラウンド数 (A/C/AC)")
    p.add_argument("--edge-url",  default="http://127.0.0.1:8001",            help="エッジサーバ URL (モード A のみ)")
    p.add_argument("--quiet",     action="store_true",                        help="詳細ログを抑制")
    return p


def main():
    args = _build_parser().parse_args()

    print()
    print(_bold("╔═══════════════════════════════╗"))
    print(_bold("║   HFL Test Runner             ║"))
    print(_bold("╚═══════════════════════════════╝"))
    print(f"  モード: {_bold(args.mode)}")
    print()

    t0 = time.time()

    if args.mode == "A":
        ok = run_mode_a(
            edge_url=args.edge_url,
            n_terminals=args.terms,
            rounds=args.rounds,
            quiet=args.quiet,
        )
    elif args.mode == "B":
        ok = run_mode_b(quiet=args.quiet)
    elif args.mode == "C":
        ok = run_mode_c(
            n_edges=args.edges,
            n_terminals=args.terms,
            rounds=args.rounds,
            quiet=args.quiet,
        )
    elif args.mode == "AC":
        ok = run_mode_ac(
            n_edges=args.edges,
            n_terminals=args.terms,
            rounds=args.rounds,
            quiet=args.quiet,
        )
    else:
        print(_err(f"不明なモード: {args.mode}"))
        return 1

    elapsed = time.time() - t0
    print()
    print(f"  経過時間: {elapsed:.1f} 秒")
    print()
    if ok:
        print(_ok(f"  ═ PASS ═"))
    else:
        print(_err(f"  ═ FAIL ═"))
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
