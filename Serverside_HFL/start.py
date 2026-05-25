#!/usr/bin/env python3
"""
HFL Server Launcher
===================
使い方:
  python start.py                           # 対話メニューで起動（先にデバッグ/本番を選択）
  python start.py --run-mode debug          # デバッグ（エッジ未指定なら1台・閾値は全1）
  python start.py --run-mode production --env shinomilab --edges 2
  python start.py --env shinomilab          # 環境を指定して即起動（shinomilab はこのPCのIPを検出）
  python start.py --env shinomilab --edges 2  # エッジ2台（既定: 中央閾値=2, エッジ閾値=2,1）
  python start.py --env localhost --edges 3 --edge-thresholds 2,2,1
  python start.py --env localhost --edges 2 --central-threshold 2 --edge-thresholds 2,1
  python start.py status                    # 起動中サーバの死活確認

起動後（対話・本番ランチャーでサーバ起動したターミナル）:
  Ctrl+L   … エッジ–端末の接続状況を表形式で表示（127.0.0.1 の中央/エッジに問い合わせ）

ラン種別 (--run-mode または対話):
  debug       … 手元試行向け。--edges 省略時はエッジ1台。閾値は（未指定なら）全エッジで1。
  production  … 実験向け既定。エッジ2台なら端末閾値 2,1、それ以外は従来どおり。

集約閾値:
  未指定かつ環境変数も未設定のとき、中央は 1（いずれかのエッジが届いた時点で即集約）。
  エッジはエッジ2台のとき既定 2,1（端末2+1）、それ以外は各エッジ 1。上書きは export または
  --central-threshold / --edge-thresholds で指定。
  注意: エッジの「端末閾値=N」は「先にアップロード完了した N 台」で集約が走る。
  同一エッジに接続する端末が N 台より多いと、遅れた端末は集約後に 423 になり
  weights_receive_start だけが増えるログになり得る（3台で閾値2なら3台目が該当しやすい）。
"""

import argparse
import asyncio
import getpass
import ipaddress
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

try:
    import httpx
    _has_httpx = True
except ImportError:
    import urllib.request
    _has_httpx = False

ROOT = Path(__file__).resolve().parent

# ------------------------------------------------------------------ #
# 環境プロファイル
# ------------------------------------------------------------------ #
PROFILES = {
    # ip はフォールバック。shinomilab のみ起動時にこのマシンのアドレスへ差し替え（後述）。
    "shinomilab": {"ip": "192.168.11.2",  "label": "研究室 LAN (192.168.11.x を検出)"},
    "hachioji":   {"ip": "192.168.0.11",  "label": "八王子 (192.168.0.11)"},
    "osaka":      {"ip": "192.168.68.69", "label": "大阪  (192.168.68.69)"},
    "localhost":  {"ip": "127.0.0.1",     "label": "同一PC (127.0.0.1)"},
}

# shinomilab 用: 端末が同一セグメントから繋ぐ想定のプレフィックス
_SHINOMILAB_NET = ipaddress.ip_network("192.168.11.0/24")
# よくあるプライマリ IF 名（複数 11.x があるときの優先順）
_SHINOMILAB_IFACE_PREF = ("en0", "en1", "eth0", "wlan0", "wlp")


def _resolve_shinomilab_host_ip(fallback: str) -> tuple[str, str]:
    """
    研究室プロファイル用に、このホストに割り当たっている IPv4 を推定する。

    1) 192.168.11.0/24 に属する非ループバックアドレスがあればそれを優先
    2) 無ければ外向き UDP のローカル終端（デフォルト経路側）
    3) それも無ければ fallback（従来の固定例: 192.168.11.2）

    戻り値: (ip, reason) reason は lab_subnet | default_route | fallback
    """
    try:
        import psutil
    except ImportError:
        psutil = None  # type: ignore

    if psutil is not None:
        hits: list[tuple[str, str]] = []
        for iface in sorted(psutil.net_if_addrs().keys()):
            if iface.startswith("lo"):
                continue
            for a in psutil.net_if_addrs().get(iface, []):
                if a.family != socket.AF_INET:
                    continue
                addr = (a.address or "").split("%")[0].strip()
                if not addr or addr.startswith("127."):
                    continue
                try:
                    if ipaddress.ip_address(addr) in _SHINOMILAB_NET:
                        hits.append((iface, addr))
                except ValueError:
                    continue
        if hits:
            for pref in _SHINOMILAB_IFACE_PREF:
                for iface, addr in hits:
                    if iface == pref:
                        return addr, "lab_subnet"
            return hits[0][1], "lab_subnet"

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.25)
        s.connect(("8.8.8.8", 80))
        addr = s.getsockname()[0]
        s.close()
        if addr and not str(addr).startswith("127."):
            return str(addr), "default_route"
    except Exception:
        pass

    return fallback, "fallback"


def _apply_shinomilab_dynamic_ip(profile_name: str, static_ip: str) -> str:
    """shinomilab のときだけ検出 IP に差し替え。それ以外は static_ip のまま。"""
    if profile_name != "shinomilab":
        return static_ip
    detected, reason = _resolve_shinomilab_host_ip(static_ip)
    how = {
        "lab_subnet": "192.168.11.x をこのホストで検出",
        "default_route": "外向き経路のローカルアドレスを使用（11.x が見つからない場合）",
        "fallback": "検出に失敗したためプロファイルの既定 IP を使用",
    }.get(reason, reason)
    print(_gray(f"  shinomilab: {how} → {detected}"))
    return detected

CENTRAL_PORT = 8000
EDGE_BASE_PORT = 8001  # 2台目は 8002、3台目は 8003 ...

# ------------------------------------------------------------------ #
# カラー設定（Windows も含め ANSI が使えない場合は無色）
# ------------------------------------------------------------------ #
_USE_COLOR = sys.stdout.isatty() and os.name != "nt" or (
    os.name == "nt" and os.environ.get("WT_SESSION")  # Windows Terminal
)

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def _bold(t):  return _c("1", t)
def _blue(t):  return _c("34", t)
def _green(t): return _c("32", t)
def _yellow(t):return _c("33", t)
def _magenta(t):return _c("35", t)
def _cyan(t):  return _c("36", t)
def _red(t):   return _c("31", t)
def _gray(t):  return _c("90", t)

EDGE_COLORS = [_green, _yellow, _magenta, _cyan]

# ------------------------------------------------------------------ #
# ユーティリティ
# ------------------------------------------------------------------ #
def _clear_line():
    if _USE_COLOR:
        print("\033[2K\r", end="", flush=True)

def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default

def _ensure_uvicorn_for_launcher() -> None:
    """
    子プロセスの uvicorn 起動にも sys.executable が使われるため、
    ランチャー自身と同じインタプリタに uvicorn が入っている必要がある。
    """
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print()
        print(_red(f"エラー: この Python に uvicorn がありません。"))
        print(_gray(f"  使用中: {sys.executable}"))
        print()
        print(_bold("対処例（仮想環境）:"))
        print(_gray("  cd Serverside_HFL"))
        print(_gray("  python3 -m venv .venv"))
        print(_gray("  source .venv/bin/activate    # Windows: .venv\\Scripts\\activate"))
        print(_gray("  pip install -r requirements.txt"))
        print(_gray("  python3 start.py"))
        print()
        raise SystemExit(1)
    try:
        import websockets  # noqa: F401
    except ImportError:
        try:
            import wsproto  # noqa: F401
        except ImportError:
            print(
                _yellow(
                    "注意: WebSocket 用ライブラリ (websockets / wsproto) が見つかりません。"
                    " エッジの /ws/updates が機能しない可能性があります。"
                    " `pip install 'uvicorn[standard]'` で requirements を入れ直してください。"
                )
            )


def _get(url: str, timeout: int = 3) -> int:
    """GETして HTTP ステータスコードを返す。失敗時は 0。"""
    try:
        if _has_httpx:
            r = httpx.get(url, timeout=timeout)
            return r.status_code
        else:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.status
    except Exception:
        return 0


# ------------------------------------------------------------------ #
# 環境選択メニュー
# ------------------------------------------------------------------ #
def select_profile() -> tuple[str, str]:
    """(profile_name, ip) を返す。"""
    print()
    print(_bold("─── 環境プロファイルを選択 ───"))
    keys = list(PROFILES.keys())
    for i, k in enumerate(keys, 1):
        print(f"  {i}. {k:<12} {_gray(PROFILES[k]['label'])}")
    print(f"  {len(keys)+1}. カスタム IP を入力")
    print()

    choice = _ask("番号を入力", "1")
    try:
        idx = int(choice) - 1
    except ValueError:
        idx = 0

    if idx == len(keys):  # カスタム
        ip = _ask("IP アドレスを入力", "127.0.0.1")
        return "custom", ip
    elif 0 <= idx < len(keys):
        k = keys[idx]
        return k, PROFILES[k]["ip"]
    else:
        print(_yellow("無効な選択です。shinomilab を使用します。"))
        return "shinomilab", PROFILES["shinomilab"]["ip"]


def select_edge_count() -> int:
    val = _ask("エッジサーバの台数", "1")
    try:
        n = int(val)
        return max(1, min(n, 8))
    except ValueError:
        return 1


def select_thresholds(
    n_edges: int,
    default_edge_thresholds: list[int],
    default_central: int,
    *,
    cli_central: Optional[int],
    cli_edge_str: str,
) -> tuple[int, list[int]]:
    """
    対話メニューで集約閾値を確認・設定する。
    CLIで明示指定済みの場合はその値を使い、メニューをスキップする。

    戻り値: (central_threshold, edge_thresholds)
    """
    # CLI で両方指定済みならスキップ
    if cli_central is not None and cli_edge_str.strip():
        return cli_central, default_edge_thresholds

    print()
    print(_bold("─── 集約閾値の設定 ───"))
    print(_gray("  中央: 何台のエッジが送信したらグローバル集約するか"))
    print(_gray("  エッジ: 何台の端末が送信したらエッジ集約するか"))
    print()

    # 中央閾値
    if cli_central is not None:
        central = cli_central
        print(f"  中央閾値 : {_green(str(central))}  {_gray('(--central-threshold で指定済み)')}")
    else:
        central_str = _ask(f"  中央集約閾値（1=いずれかのエッジで即集約、{n_edges}=全エッジ待ち）", str(default_central))
        try:
            central = max(1, int(central_str))
        except ValueError:
            central = default_central
        print(_gray(f"  → 中央閾値 = {central}"))

    # エッジ閾値
    if cli_edge_str.strip():
        edge_thresholds = default_edge_thresholds
        print(f"  エッジ閾値: {_green(str(edge_thresholds))}  {_gray('(--edge-thresholds で指定済み)')}")
    else:
        print()
        if n_edges == 1:
            default_str = str(default_edge_thresholds[0])
            val = _ask(f"  エッジ1 の端末集約閾値", default_str)
            try:
                edge_thresholds = [max(1, int(val))]
            except ValueError:
                edge_thresholds = default_edge_thresholds
        else:
            default_str = ",".join(str(t) for t in default_edge_thresholds)
            print(f"  エッジ{n_edges}台分をカンマ区切りで入力してください（例: {default_str}）")
            val = _ask(f"  エッジ閾値", default_str)
            parts = [p.strip() for p in val.split(",") if p.strip()]
            if len(parts) == n_edges:
                try:
                    edge_thresholds = [max(1, int(p)) for p in parts]
                except ValueError:
                    edge_thresholds = default_edge_thresholds
            else:
                print(_yellow(f"  入力数がエッジ台数と合いません。既定値 {default_edge_thresholds} を使用します。"))
                edge_thresholds = default_edge_thresholds
        print(_gray(f"  → エッジ閾値 = {edge_thresholds}"))

    return central, edge_thresholds


def select_run_mode(cli_run_mode: str) -> str:
    """
    デバッグ試行か本番実験かを CLI または対話で決める。
    戻り値: \"debug\" | \"production\"
    """
    raw = (cli_run_mode or "").strip().lower()
    if raw in ("debug", "d", "dev"):
        return "debug"
    if raw in ("production", "prod", "p", "experiment", "exp"):
        return "production"
    if raw:
        raise SystemExit(
            f"無効な --run-mode: {cli_run_mode!r}。使える値: debug, production"
        )

    print()
    print(_bold("─── ラン種別を選択 ───"))
    print(f"  {_cyan('D')} … デバッグラン（単発確認・修正検証向け）")
    print(f"      {_gray('エッジ台数未指定なら 1 台。端末集約閾値は（未指定なら）全エッジで 1。')}")
    print(f"  {_cyan('P')} … 本番実験ラン（実機実験・採用データ向けの既定）")
    print(f"      {_gray('エッジ台数は次の質問で指定。エッジ2台なら端末閾値 2,1 など従来の既定。')}")
    print()
    choice = _ask("D または P", "D").strip().lower()
    if choice in ("p", "prod", "production", "本番", "2"):
        return "production"
    if choice in ("d", "debug", "dev", "デバッグ", "1", ""):
        return "debug"
    print(_yellow("解釈できません。デバッグランとして起動します。"))
    return "debug"


def select_ai_advisor() -> dict:
    """
    AI アドバイザーの設定を対話式で選択する。
    戻り値: {"provider": "claude"|"gemini"|"", "api_key": "..."}
    使わない場合は {"provider": "", "api_key": ""}
    """
    print()
    print(_bold("─── AI アドバイザー ───"))
    print("  実験中に AI がリアルタイムで分析・警告・レポートを出力します。")
    print(f"  {_gray('（API キーが必要です。その場だけ使われ、ファイルには保存しません）')}")
    print()

    use = _ask("AI アドバイザーを使いますか？ [y/N]", "N").lower()
    if use not in ("y", "yes"):
        print(f"  {_gray('スキップしました。')}")
        return {"provider": "", "api_key": ""}

    # プロバイダ選択
    print()
    print("  プロバイダを選択してください:")
    print("    1. Claude  (Anthropic)")
    print("    2. Gemini  (Google)")
    choice = _ask("  番号", "1")

    if choice == "2":
        provider = "gemini"
        key_label = "Gemini API キー (AIza...)"
        key_env   = "GEMINI_API_KEY"
    else:
        provider = "claude"
        key_label = "Anthropic API キー (sk-ant-...)"
        key_env   = "ANTHROPIC_API_KEY"

    # 既に環境変数にセットされていればスキップ
    existing = os.environ.get(key_env, "")
    if existing:
        print(f"  {_green('✓')} {key_env} は既に設定されています。")
        return {"provider": provider, "api_key": existing}

    # API キー入力（入力中は非表示）
    print()
    try:
        api_key = getpass.getpass(f"  {key_label}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        print(f"  {_gray('スキップしました。')}")
        return {"provider": "", "api_key": ""}

    if not api_key.strip():
        print(f"  {_yellow('APIキーが空のため AI アドバイザーを無効にします。')}")
        return {"provider": "", "api_key": ""}

    print(f"  {_green('✓')} API キーを受け取りました。")
    return {"provider": provider, "api_key": api_key.strip()}


# ------------------------------------------------------------------ #
# サーバー起動・ログストリーミング
# ------------------------------------------------------------------ #
def _stream_logs(proc: subprocess.Popen, prefix: str, color_fn):
    """プロセスの stdout/stderr を色付きプレフィックス付きで出力するスレッド。"""
    tag = color_fn(f"[{prefix}]")
    for line in proc.stdout:
        text = line.rstrip()
        if text:
            print(f"{tag} {text}", flush=True)


def _build_env(
    ip: str,
    profile_name: str,
    edge_id=None,
    ai_cfg: dict = None,
    *,
    run_mode: str = "production",
) -> dict:
    env = os.environ.copy()
    env["HFL_ENV"]            = profile_name
    env["HFL_RUN_MODE"]       = run_mode
    env["CENTRAL_SERVER_URL"] = f"http://{ip}:{CENTRAL_PORT}"
    env["PYTHONUNBUFFERED"]   = "1"
    if edge_id:
        env["EDGE_SERVER_ID"] = edge_id
    if ai_cfg and ai_cfg.get("provider"):
        provider = ai_cfg["provider"]
        api_key  = ai_cfg["api_key"]
        env["AI_ADVISOR_PROVIDER"] = provider
        if provider == "claude":
            env["ANTHROPIC_API_KEY"] = api_key
        elif provider == "gemini":
            env["GEMINI_API_KEY"] = api_key
    return env


def _compute_edge_thresholds(
    n_edges: int,
    edge_thresholds_arg: str,
    *,
    run_mode: str = "production",
) -> list[int]:
    """
    各エッジの端末集約閾値（何台の端末更新でエッジ集約するか）。
    --edge-thresholds が空のとき:
      production かつエッジ2台: [2,1]（研究デフォルトの 2+1 端末）
      production かつそれ以外: 全1
      debug: 常に全1（手元試行向け）
    """
    arg = (edge_thresholds_arg or "").strip()
    if arg:
        parts = [p.strip() for p in arg.split(",") if p.strip() != ""]
        if len(parts) != n_edges:
            raise SystemExit(
                f"--edge-thresholds にはカンマ区切りでちょうど {n_edges} 個の整数が必要です（例: 2,1）"
            )
        try:
            return [int(p) for p in parts]
        except ValueError as e:
            raise SystemExit(f"--edge-thresholds の解析に失敗しました: {e}") from e
    if run_mode == "debug":
        return [1] * n_edges
    if n_edges == 2:
        return [2, 1]
    return [1] * n_edges


def start_servers(
    profile_name: str,
    ip: str,
    n_edges: int,
    ai_cfg: dict = None,
    *,
    central_threshold: Optional[int] = None,
    edge_thresholds: Optional[List[int]] = None,
    run_mode: str = "production",
):
    procs: list[tuple[subprocess.Popen, str]] = []

    if edge_thresholds is None:
        edge_thresholds = [1] * n_edges
    ct = central_threshold if central_threshold is not None else 1

    print()
    print(_bold("─── サーバーを起動しています ───"))
    mode_label = "デバッグ" if run_mode == "debug" else "本番実験"
    print(_gray(f"  ラン種別: {mode_label}  (HFL_RUN_MODE={run_mode})"))
    print(
        _gray(
            f"  集約閾値: 中央={ct}（エッジ{ct}台分そろったらグローバル集約） "
            f"/ 各エッジ={edge_thresholds}"
        )
    )

    # ── 中央サーバ ──
    cmd_c = [
        sys.executable, "-m", "uvicorn",
        "central_server.main:app",
        "--host", "0.0.0.0",
        "--port", str(CENTRAL_PORT),
        "--log-level", "info",
    ]
    # AI アドバイザーの設定は中央サーバにのみ渡す
    env_c = _build_env(ip, profile_name, ai_cfg=ai_cfg, run_mode=run_mode)
    if "CENTRAL_AGGREGATION_THRESHOLD" not in env_c:
        env_c["CENTRAL_AGGREGATION_THRESHOLD"] = str(ct)
    proc_c = subprocess.Popen(
        cmd_c, cwd=str(ROOT), env=env_c,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    procs.append((proc_c, "中央"))
    threading.Thread(
        target=_stream_logs, args=(proc_c, "中央", _blue),
        daemon=True,
    ).start()
    print(f"  {_blue('●')} 中央サーバ  :{CENTRAL_PORT}  起動中...")

    # 中央サーバが立ち上がるのを待つ
    _wait_ready(f"http://127.0.0.1:{CENTRAL_PORT}/healthz", label="中央サーバ")

    # ── エッジサーバ（n台）──
    for i in range(n_edges):
        port = EDGE_BASE_PORT + i
        edge_id = f"edge-server-{i+1:02d}"
        color = EDGE_COLORS[i % len(EDGE_COLORS)]

        cmd_e = [
            sys.executable, "-m", "uvicorn",
            "edge_server.main:app",
            "--host", "0.0.0.0",
            "--port", str(port),
            "--log-level", "info",
        ]
        env_e = _build_env(ip, profile_name, edge_id=edge_id, run_mode=run_mode)
        env_e["EDGE_URL"] = f"http://{ip}:{port}"
        eth = edge_thresholds[i]
        if "EDGE_AGGREGATION_THRESHOLD" not in env_e:
            env_e["EDGE_AGGREGATION_THRESHOLD"] = str(eth)
        if "EDGE_AGGREGATION_THRESHOLD_OVERRIDE" not in env_e:
            env_e["EDGE_AGGREGATION_THRESHOLD_OVERRIDE"] = str(eth)
        proc_e = subprocess.Popen(
            cmd_e, cwd=str(ROOT), env=env_e,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        label = f"エッジ{i+1} ({edge_id})"
        procs.append((proc_e, label))
        threading.Thread(
            target=_stream_logs, args=(proc_e, f"エッジ{i+1}", color),
            daemon=True,
        ).start()
        print(f"  {color('●')} エッジサーバ{i+1} :{port}  ({edge_id})")
        _wait_ready(f"http://127.0.0.1:{port}/healthz", label=f"エッジサーバ{i+1}")

    # ── 起動完了サマリ ──
    edge_ports = [EDGE_BASE_PORT + i for i in range(n_edges)]

    print()
    print(_bold("─── 起動完了 ───"))
    print(f"  環境     : {_bold(profile_name)}  (中央サーバ → http://{ip}:{CENTRAL_PORT})")
    print(f"  中央     : http://127.0.0.1:{CENTRAL_PORT}")
    for i in range(n_edges):
        port = EDGE_BASE_PORT + i
        print(f"  エッジ{i+1}  : http://127.0.0.1:{port}")
    if ai_cfg and ai_cfg.get("provider"):
        print(f"  AI       : {_magenta(ai_cfg['provider'])} アドバイザー有効")
    else:
        print(f"  AI       : {_gray('無効')}")
    print()
    print(_gray("  Ctrl+C : 全サーバを停止（トレース自動回収）"))
    print(_gray("  Ctrl+G : グレースフルリセット（ログ保存 → 学習状態を初期化）"))
    print(_gray("  Ctrl+D : 失敗試行データ削除（リセット + ログ/受信データ/配布モデル削除）"))
    print(_gray("  Ctrl+L : エッジ–端末トポロジをこのターミナルに表示（表）"))
    print()

    # ── 端末トレース自動開始 ──
    trace_started = False
    try:
        from scripts.perfetto import conductor as _conductor
        print(_bold("─── 端末トレース開始 ───"))
        _started = _conductor.start_all()
        trace_started = bool(_started)
        if trace_started:
            print(_green("  トレース録画中（Ctrl+C で停止時に自動回収）"))
        else:
            print(_yellow("  ⚠ トレース開始失敗（端末未接続の可能性）。サーバは続行します"))
    except Exception as _e:
        print(_yellow(f"  ⚠ トレース自動開始をスキップ: {_e}"))
    print()

    # ── キーボードリスナー起動 ──
    _kbd_thread, _old_term = _start_keyboard_listener(edge_ports, CENTRAL_PORT)

    # ── Ctrl+C まで待機 ──
    try:
        while True:
            time.sleep(1)
            # いずれかのプロセスが落ちたら通知
            for proc, name in procs:
                if proc.poll() is not None:
                    print(_red(f"\n[警告] {name} が終了しました (code={proc.returncode})"))
    except KeyboardInterrupt:
        print()
        print(_bold("停止中..."))
    finally:
        # ── 端末トレース自動回収 ──
        if trace_started:
            try:
                print(_bold("─── 端末トレース回収 ───"))
                _conductor.stop_all()
            except Exception as _e:
                print(_yellow(f"  ⚠ トレース回収失敗: {_e}"))

        # ターミナル設定を確実に復元
        if _old_term is not None:
            try:
                import termios as _termios
                _termios.tcsetattr(sys.stdin.fileno(), _termios.TCSADRAIN, _old_term)
            except Exception:
                pass
        for proc, name in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc, name in procs:
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        print("全サーバを停止しました。")


def _wait_ready(url: str, label: str, timeout: int = 20):
    start = time.time()
    while time.time() - start < timeout:
        code = _get(url)
        if 0 < code < 500:
            print(f"    {_green('✓')} {label} 準備完了")
            return
        time.sleep(0.5)
    print(f"    {_yellow('⚠')} {label} タイムアウト（起動に時間がかかっています）")


# ------------------------------------------------------------------ #
# グレースフルリセット
# ------------------------------------------------------------------ #
def _print_topology_dashboard(edge_ports: list, central_port: int) -> None:
    """
    中央の /ops/edge-terminal-topology/data を優先し、失敗時は各エッジの
    /admin/topology_snapshot を 127.0.0.1 から直接取得してターミナルに表を出す。
    """
    print()
    print(_bold("─── エッジ–端末トポロジ（スナップショット）───"))
    url = f"http://127.0.0.1:{central_port}/ops/edge-terminal-topology/data"
    data = None
    try:
        if _has_httpx:
            r = httpx.get(url, timeout=12.0)
            if r.status_code == 200:
                data = r.json()
            else:
                print(_yellow(f"  中央 {url} → HTTP {r.status_code}（ローカルエッジのみ表示します）"))
        else:
            import json as _json
            import urllib.request

            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=12) as resp:
                if resp.status == 200:
                    data = _json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        print(_yellow(f"  中央からの取得をスキップ: {exc}"))

    if data:
        _render_topology_payload_text(data)
    else:
        _render_topology_from_localhost_edges(edge_ports)

    print(_gray("  （遅い端末はエッジ閾値=2で先に2台が集約されると 423 / weights_receive_start のみ、とログに出ることがあります）"))
    print()


def _render_topology_payload_text(data: dict) -> None:
    c = data.get("central") or {}
    print(_gray(f"  中央 global_round={c.get('global_model_round')}  登録エッジ URL: {c.get('registered_edge_urls')}"))
    rows = data.get("edges") or []
    if not rows:
        print(_yellow("  登録エッジがありません。"))
        return
    w_url = 28
    print()
    hdr = f"  {'edge_url':<{w_url}} {'OK':^4} {'edge_id':<16} {'allow':<10} {'thr':>3} {'rnd':>3} 端末(ledger) / メモリ更新(抜粋)"
    print(_cyan(hdr))
    print(_gray("  " + "-" * min(120, len(hdr) + 20)))
    for row in rows:
        eu = str(row.get("edge_url") or "")[:w_url]
        if not row.get("ok"):
            err = str(row.get("error") or "?")[:80]
            print(f"  {eu:<{w_url}} {_red('no'):^4} {_gray(err)}")
            continue
        d = row.get("data") or {}
        eid = str(d.get("edge_id") or "")[:16]
        am = d.get("allow_mode") or ""
        allow = "全端末" if am == "all" else "制限"
        thr = d.get("aggregation_threshold", "")
        rnd = d.get("current_round", "")
        led = d.get("terminal_ledgers") or {}
        tids = sorted(led.keys())
        led_s = ",".join(tids[:6]) + ("…" if len(tids) > 6 else "") if tids else "—"
        upd = d.get("updates_in_memory_by_round") or []
        upd_s = "; ".join(
            f"{u.get('round_id')}/{u.get('terminal_id')}" for u in upd[:4]
        )
        if len(upd) > 4:
            upd_s += "…"
        if not upd_s:
            upd_s = "—"
        print(f"  {eu:<{w_url}} {_green('ok'):^4} {eid:<16} {allow:<10} {str(thr):>3} {str(rnd):>3} {led_s}")
        print(_gray(f"      └ updates: {upd_s}"))
    print()


def _render_topology_from_localhost_edges(edge_ports: list) -> None:
    import json as _json

    for port in edge_ports:
        u = f"http://127.0.0.1:{port}/admin/topology_snapshot"
        try:
            if _has_httpx:
                r = httpx.get(u, timeout=5.0)
                body = r.json() if r.status_code == 200 else {"_error": r.status_code}
            else:
                import json as _json
                import urllib.request

                req = urllib.request.Request(u, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    body = _json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            body = {"_error": str(exc)}
        print(_cyan(f"  [:{port}]"), _json.dumps(body, ensure_ascii=False)[:500])
    print()


def _do_graceful_reset(edge_ports: list, central_port: int) -> None:
    """全サーバに POST /admin/graceful_reset を送信してログ・状態を保存し初期化する。"""
    print()
    print(_bold("─── グレースフルリセット実行中 ───"))
    targets = [
        (f"中央サーバ  (:{central_port})", f"http://127.0.0.1:{central_port}/admin/graceful_reset"),
    ]
    for port in edge_ports:
        targets.append((f"エッジサーバ (:{port})", f"http://127.0.0.1:{port}/admin/graceful_reset"))

    for label, url in targets:
        try:
            if _has_httpx:
                r = httpx.post(url, timeout=10)
                ok = r.status_code == 200
                body = r.json() if ok else {}
            else:
                import json
                import urllib.request
                req = urllib.request.Request(url, method="POST", data=b"")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    ok = resp.status == 200
                    body = json.loads(resp.read())
            if ok:
                print(f"  {_green('✓')} {label}: {body.get('message', 'リセット完了')}")
            else:
                print(f"  {_yellow('⚠')} {label}: 応答 HTTP {r.status_code if _has_httpx else '?'}")
        except Exception as exc:
            print(f"  {_red('✗')} {label}: {exc}")

    print()
    print(_green("  グレースフルリセット完了。学習状態を学習開始前に戻しました。"))
    print()


def _do_purge_failed_trial(edge_ports: list, central_port: int) -> None:
    """失敗した試行のデータを安全に削除する。

    処理:
      1. グレースフルリセット（サーバ状態初期化 + WebSocket全切断）
      2. 最新試行のログ・受信データ・配布モデルを削除
    """
    import shutil
    print()
    print(_bold("─── 失敗試行データ削除 ───"))
    print(_yellow("  直近の試行データを削除して学習状態をリセットします。"))
    print()

    # 1. グレースフルリセット（サーバ状態 + WebSocket切断）
    _do_graceful_reset(edge_ports, central_port)

    # 2. ファイルシステム上のデータ削除
    root = Path(__file__).resolve().parent
    deleted_items: list = []
    errors: list = []

    # 2a. received_edges/ の全ラウンドディレクトリを削除
    received = root / "received_edges"
    if received.exists():
        for rdir in sorted(received.iterdir()):
            if rdir.is_dir() and rdir.name.startswith("r"):
                try:
                    shutil.rmtree(rdir)
                    deleted_items.append(f"received_edges/{rdir.name}")
                except Exception as e:
                    errors.append(f"received_edges/{rdir.name}: {e}")
        # マーカーファイルもリセット
        for marker in ["processed_data_ids.json"]:
            mf = received / marker
            if mf.exists():
                try:
                    mf.write_text("{}", encoding="utf-8")
                    deleted_items.append(f"received_edges/{marker} (reset)")
                except Exception as e:
                    errors.append(f"{marker}: {e}")

    # 2b. dist/ の全配布パッケージを削除
    dist_dir = root / "dist"
    if dist_dir.exists():
        for ddir in sorted(dist_dir.iterdir()):
            if ddir.is_dir():
                try:
                    shutil.rmtree(ddir)
                    deleted_items.append(f"dist/{ddir.name}")
                except Exception as e:
                    errors.append(f"dist/{ddir.name}: {e}")

    # 2c. edge_server/runs/ のランディレクトリを削除
    runs_dir = root / "edge_server" / "runs"
    if runs_dir.exists():
        for rdir in sorted(runs_dir.iterdir()):
            if rdir.is_dir():
                try:
                    shutil.rmtree(rdir)
                    deleted_items.append(f"edge_server/runs/{rdir.name}")
                except Exception as e:
                    errors.append(f"runs/{rdir.name}: {e}")

    # 2d. state/archives/ の最新アーカイブを削除
    archives = root / "state" / "archives"
    if archives.exists():
        archive_dirs = sorted([d for d in archives.iterdir() if d.is_dir()])
        if archive_dirs:
            latest = archive_dirs[-1]
            try:
                shutil.rmtree(latest)
                deleted_items.append(f"state/archives/{latest.name}")
            except Exception as e:
                errors.append(f"archives/{latest.name}: {e}")

    # 2e. training_metrics.db をクリア
    metrics_db = received / "training_metrics.db"
    if metrics_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(metrics_db))
            conn.execute("DELETE FROM metrics")
            conn.commit()
            conn.close()
            deleted_items.append("training_metrics.db (cleared)")
        except Exception as e:
            errors.append(f"training_metrics.db: {e}")

    # 結果表示
    if deleted_items:
        print(f"  {_green('削除完了')} ({len(deleted_items)} 項目):")
        for item in deleted_items[:10]:
            print(f"    - {item}")
        if len(deleted_items) > 10:
            print(f"    ... 他 {len(deleted_items) - 10} 項目")
    else:
        print(f"  {_yellow('削除対象なし')}")

    if errors:
        print(f"  {_red('エラー')} ({len(errors)} 件):")
        for err in errors:
            print(f"    - {err}")

    print()
    print(_green("  失敗試行のデータを削除しました。次の試行を開始できます。"))
    print()


def _do_inference_snapshot() -> None:
    """グローバルモデルをロードして全端末の AP 推薦結果を表示する。"""
    import math
    import unicodedata
    import json
    import os
    import glob

    def _cjk_ljust(s: str, width: int) -> str:
        dw = sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in s)
        return s + ' ' * max(0, width - dw)

    print()
    
    round_num = "?"
    try:
        state_file = ROOT / "state" / "round_state.json"
        if state_file.exists():
            with open(state_file, encoding="utf-8") as f:
                st = json.load(f)
                round_num = st.get("round", "?")
    except Exception:
        pass

    print(_bold(f"─── AP 切り替え推論スナップショット（ラウンド {round_num}） ───"))

    model_path = ROOT / "state" / "global_model_mobile.pt"
    app_json_path = ROOT / "app.json"

    try:
        with open(app_json_path, encoding="utf-8") as f:
            apps = json.load(f)
    except Exception as e:
        print(f"  {_red('✗')} app.json 読み込み失敗: {e}")
        return

    try:
        import torch as _torch
        sd = _torch.load(str(model_path), map_location="cpu", weights_only=False)
        if not isinstance(sd, dict):
            raise RuntimeError(f"state_dict が dict ではありません: {type(sd)}")
    except Exception as e:
        print(f"  {_red('✗')} モデル読み込み失敗 ({model_path}): {e}")
        return

    try:
        import numpy as _np
        W1  = sd["layer1.weight"].numpy().astype(_np.float32)
        b1  = sd["layer1.bias"].numpy().astype(_np.float32)
        gn1 = sd["norm1.weight"].numpy().astype(_np.float32)
        bn1 = sd["norm1.bias"].numpy().astype(_np.float32)
        W2  = sd["layer2.weight"].numpy().astype(_np.float32)
        b2  = sd["layer2.bias"].numpy().astype(_np.float32)
        gn2 = sd["norm2.weight"].numpy().astype(_np.float32)
        bn2 = sd["norm2.bias"].numpy().astype(_np.float32)
        Wo  = sd["layer3.weight"].numpy().astype(_np.float32)
        bo  = sd["layer3.bias"].numpy().astype(_np.float32)
        hidden      = b1.shape[0]
        output_size = bo.shape[0]
        input_size  = W1.shape[1]
    except KeyError as e:
        print(f"  {_red('✗')} 予期しない state_dict キー: {e}")
        print(f"  利用可能なキー: {list(sd.keys())}")
        return

    def _layer_norm(x, gamma, beta, eps=1e-5):
        mu  = x.mean()
        var = ((x - mu) ** 2).mean()
        return gamma * (x - mu) / math.sqrt(float(var) + eps) + beta

    def _relu(x):
        return x * (x > 0)

    def _softmax(x):
        shifted = [v - max(x) for v in x]
        exps = [math.exp(v) for v in shifted]
        s = sum(exps)
        return [v / s for v in exps]

    def _forward(inp):
        x = _np.array(inp, dtype=_np.float32)
        with _np.errstate(divide="ignore", over="ignore", invalid="ignore"):
            h1 = W1 @ x + b1
            h1 = _layer_norm(h1, gn1, bn1)
            h1 = _relu(h1)
            h2 = W2 @ h1 + b2
            h2 = _layer_norm(h2, gn2, bn2)
            h2 = _relu(h2)
            logits = (Wo @ h2 + bo).tolist()
        probs = _softmax(logits)
        return logits, probs

    terminal_data = {}
    logs_dir = ROOT / "logs" / "time_records"
    central_logs = sorted(glob.glob(str(logs_dir / "central_server_*.log")), reverse=True)

    # --- データソース1: DB (training_metrics.db) から最新レコードを取得 ---
    try:
        import sqlite3
        db_path = ROOT / "received_edges" / "training_metrics.db"
        if db_path.exists():
            with sqlite3.connect(str(db_path), timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                # カラム存在確認
                cur = conn.execute("PRAGMA table_info(metrics)")
                cols = {row[1] for row in cur.fetchall()}
                if "terminal_id" in cols:
                    select_cols = ["terminal_id", "app_type", "edge_id", "round", "accuracy", "loss",
                                   "satisfaction_before", "satisfaction_after"]
                    if "tp_measured_mbps" in cols:
                        select_cols.append("tp_measured_mbps")
                    if "rtt_measured_ms" in cols:
                        select_cols.append("rtt_measured_ms")
                    rows = conn.execute(f"""
                        SELECT {', '.join(select_cols)}
                        FROM metrics
                        WHERE terminal_id IS NOT NULL AND terminal_id != ''
                        ORDER BY id DESC
                    """).fetchall()
                    seen = set()
                    for row in rows:
                        tid = row["terminal_id"]
                        if tid in seen:
                            continue
                        seen.add(tid)
                        terminal_data[tid] = dict(row)
    except Exception as e:
        logger.debug(f"DB参照スキップ: {e}")

    # --- データソース2: ログからフォールバック（DBが空の場合） ---
    if not terminal_data and central_logs:
        latest_log = central_logs[0]
        try:
            with open(latest_log, encoding="utf-8") as f:
                for line in f:
                    if "training_metrics_received:" in line:
                        json_str = line.split("training_metrics_received:", 1)[1].strip()
                        try:
                            record = json.loads(json_str)
                            tid = record.get("terminal_id") or record.get("edge_id")
                            if tid:
                                # 端末IDごとに最新のレコードを保持（上書き）
                                terminal_data[tid] = record
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            logger.warning(f"最新ログの解析に失敗しました: {e}")

    # --- app_type 補完: training_metrics_last や index.jsonl から抽出 ---
    for tid, record in terminal_data.items():
        app_t = record.get("app_type")
        if not app_t or app_t == "unknown" or app_t == "null":
            # training_metrics_last (ネスト内) から取得
            tml = record.get("training_metrics_last")
            if isinstance(tml, dict) and tml.get("app_type"):
                record["app_type"] = tml["app_type"]
            elif isinstance(tml, str):
                try:
                    tml_parsed = json.loads(tml)
                    if isinstance(tml_parsed, dict) and tml_parsed.get("app_type"):
                        record["app_type"] = tml_parsed["app_type"]
                except Exception:
                    pass

    ap_names = [f"AP-{chr(65+i)}" for i in range(output_size)]  # Fix#4: output_size から動的生成

    # Fix#1: 英語→日本語のアプリ名マッピング（端末は英語で送信、app.json は日本語）
    _APP_NAME_ALIASES = {
        "browser": "ブラウザ", "ブラウザ": "ブラウザ",
        "video":   "動画",     "動画":   "動画",
        "call":    "通話",     "通話":   "通話",
        "other":   "配信",     "配信":   "配信",
        "streaming": "配信",
    }
    app_types = {app.get("appType", f"app{i}"): i for i, app in enumerate(apps)}

    def _resolve_app_idx(app_type_raw: str) -> int:
        """端末から受け取った app_type を app.json のインデックスに解決する。"""
        normalized = app_type_raw.strip().lower()
        # まず app.json のキーに直接マッチ
        if app_type_raw in app_types:
            return app_types[app_type_raw]
        # エイリアスで日本語に変換してから検索
        jp_name = _APP_NAME_ALIASES.get(normalized)
        if jp_name and jp_name in app_types:
            return app_types[jp_name]
        # 部分一致
        for key, idx in app_types.items():
            if normalized in key.lower() or key.lower() in normalized:
                return idx
        return 0

    # Fix#3: 入力正規化パラメータの読み込み（.norm.json）
    norm_mean = None
    norm_std = None
    norm_path = Path(str(model_path) + ".norm.json")
    if norm_path.exists():
        try:
            import re as _re
            ntxt = norm_path.read_text(encoding="utf-8")
            m = _re.search(r'"mean"\s*:\s*\[(.*?)\]', ntxt)
            s = _re.search(r'"std"\s*:\s*\[(.*?)\]', ntxt)
            if m:
                norm_mean = [float(v.strip()) for v in m.group(1).split(",")]
            if s:
                norm_std = [float(v.strip()) for v in s.group(1).split(",")]
        except Exception as e:
            print(f"  {_yellow('⚠')} norm.json の読み込みに失敗: {e}")

    # Fix#6: one_hot の次元をモデル入力サイズから算出
    n_app_features = input_size - 2  # 先頭2次元 = tp, rtt
    if n_app_features < 1:
        n_app_features = len(apps)

    # min-max 正規化定数（Sim_HFL と同一値、norm.json がない場合のフォールバック）
    _FALLBACK_TP_MAX  = 60.0
    _FALLBACK_RTT_MAX = 2000.0

    def _build_input(tp_val, rtt_val, app_idx_val):
        """入力ベクトルを構築する。正規化とone_hot次元をモデルに合わせる。"""
        tp_in, rtt_in = tp_val, rtt_val
        if norm_mean and norm_std and len(norm_mean) >= 2 and len(norm_std) >= 2:
            # z-score 正規化（norm.json がある場合 — Android 実機と同一）
            tp_in  = (tp_val  - norm_mean[0]) / (norm_std[0] if norm_std[0] > 1e-8 else 1.0)
            rtt_in = (rtt_val - norm_mean[1]) / (norm_std[1] if norm_std[1] > 1e-8 else 1.0)
        else:
            # min-max 正規化フォールバック（Sim_HFL の訓練データと同一方式）
            tp_in  = min(max(tp_val  / _FALLBACK_TP_MAX,  0.0), 1.0)
            rtt_in = min(max(rtt_val / _FALLBACK_RTT_MAX, 0.0), 1.0)
        one_hot = [1.0 if i == app_idx_val else 0.0 for i in range(n_app_features)]
        return [tp_in, rtt_in] + one_hot

    # Fix#2: 現在AP判定 — 環境変数で AP 名パターンを設定可能に
    ap_patterns = os.environ.get("AP_SSID_PATTERNS", "").split(",") if os.environ.get("AP_SSID_PATTERNS") else []

    def _detect_current_ap(record):
        """端末レコードから現在APを推定する。"""
        # ap_index が直接記録されていればそれを使う
        ap_idx = record.get("ap_index")
        if ap_idx is not None:
            try:
                return ap_names[int(ap_idx)] if int(ap_idx) < len(ap_names) else f"AP-{ap_idx}"
            except (ValueError, IndexError):
                pass
        # SSID パターンマッチ
        router = record.get("virtual_router_id", "") or ""
        if ap_patterns:
            for i, pat in enumerate(ap_patterns):
                if pat.strip() and pat.strip() in router:
                    return ap_names[i] if i < len(ap_names) else f"AP-{i}"
        # デフォルト: 既知のSSIDパターン
        if "A24" in router or "AP-A" in router:
            return "AP-A"
        if "B" in router or "AP-B" in router:
            return "AP-B"
        return "?"

    # ヘッダー行を出力サイズに合わせて生成
    ap_headers = "   ".join(f"{name:>6}" for name in ap_names)
    print(f"\n  {'端末':<14} {'現AP':<5}  {_cjk_ljust('アプリ', 8)} {'TP実測':>8}   {'RTT実測':>7}    {ap_headers}  {'推薦':>4}  {'根拠'}")
    print("  " + "─" * (70 + 9 * output_size))

    delta_threshold = 0.15
    norm_label = "(z-score)" if norm_mean else "(生値)"

    if not terminal_data:
        # Fix#5: フォールバック時は「参考値」と明記
        print(f"  {_yellow('⚠')} 端末データなし。needTP/needRTT による参考推論です（実測値と異なります）")
        for app_idx, app in enumerate(apps):
            app_name  = app.get("appType", f"app{app_idx}")
            need_tp   = float(app.get("needTP", 0))
            need_rtt  = float(app.get("needRTT", 0))
            inp = _build_input(need_tp, need_rtt, app_idx)
            try:
                _, probs = _forward(inp)
                best_ap = max(range(output_size), key=lambda i: probs[i])  # Fix#4: argmax
                top2 = sorted(range(output_size), key=lambda i: probs[i], reverse=True)
                score_diff = probs[top2[0]] - probs[top2[1]] if len(top2) >= 2 else 1.0
                rec = _green(ap_names[best_ap])
                reason = f"Δ={score_diff:.3f}" + (" (差小)" if score_diff < delta_threshold else "") + " ※参考値"
                prob_cols = "   ".join(f"{probs[i]*100:>5.1f}%" for i in range(output_size))
                print(f"  {_cjk_ljust('(参考)', 14)} {'?':<5}  {_cjk_ljust(app_name, 8)} {need_tp:>5.0f}Mbps {need_rtt:>5.0f}ms    {prob_cols}  {rec}  {reason}")
            except Exception:
                pass
    else:
        for tid in sorted(terminal_data.keys()):
            record = terminal_data[tid]
            app_type = record.get("app_type", "unknown")
            tp = float(record.get("tp_measured_mbps") or 0.0)
            rtt = float(record.get("rtt_measured_ms") or 0.0)
            current_ap = _detect_current_ap(record)  # Fix#2

            app_idx = _resolve_app_idx(app_type)  # Fix#1
            inp = _build_input(tp, rtt, app_idx)  # Fix#3 + Fix#6

            try:
                _, probs = _forward(inp)
                best_ap = max(range(output_size), key=lambda i: probs[i])  # Fix#4: argmax
                top2 = sorted(range(output_size), key=lambda i: probs[i], reverse=True)
                score_diff = probs[top2[0]] - probs[top2[1]] if len(top2) >= 2 else 1.0
                rec = _green(ap_names[best_ap])
                reason = f"Δ={score_diff:.3f}" + (" (差小)" if score_diff < delta_threshold else "")

                prob_cols = "   ".join(f"{probs[i]*100:>5.1f}%" for i in range(output_size))
                print(f"  {tid:<14} {current_ap:<5}  {_cjk_ljust(app_type, 8)} {tp:>5.1f}Mbps {rtt:>5.0f}ms    {prob_cols}  {rec}  {reason}")
            except Exception as e:
                print(f"  {tid:<14} 推論エラー: {e}")

    print()
    print(f"  注: Δ < {delta_threshold} (deltaThreshold) は切り替え閾値未満")
    print(f"  モデル: {model_path.name}  arch: Linear({input_size}→{hidden})→LN→ReLU×2→Linear({hidden}→{output_size})")
    print(f"  入力正規化: {norm_label}  one_hot次元: {n_app_features}  出力AP数: {output_size}")
    if central_logs: print(f"  データソース: {Path(central_logs[0]).name}")
    print()


def _start_keyboard_listener(edge_ports: list, central_port: int):
    """
    バックグラウンドスレッドで Ctrl+G / Ctrl+L / Ctrl+I を監視する。
    Ctrl+G → 全サーバへグレースフルリセット
    Ctrl+L → エッジ–端末トポロジをターミナルに表示
    Ctrl+I → グローバルモデル推論スナップショット（Tab キーと同値）
    Ctrl+C → SIGINT（通常の停止）
    macOS / Linux 専用（Windows は無効）。

    戻り値: (thread, old_termios_settings | None)
    """
    if os.name == "nt" or not sys.stdin.isatty():
        return None, None

    try:
        import select as _select
        import termios as _termios
        import tty as _tty
    except ImportError:
        return None, None

    fd = sys.stdin.fileno()
    try:
        old_settings = _termios.tcgetattr(fd)
    except Exception:
        return None, None

    def _listen():
        try:
            _tty.setraw(fd)
            while True:
                try:
                    r, _, _ = _select.select([fd], [], [], 1.0)
                except Exception:
                    break
                if not r:
                    continue
                try:
                    ch = os.read(fd, 1)
                except Exception:
                    break
                if ch == b'\x07':  # Ctrl+G → グレースフルリセット
                    try:
                        _termios.tcsetattr(fd, _termios.TCSADRAIN, old_settings)
                    except Exception:
                        pass
                    _do_graceful_reset(edge_ports, central_port)
                    try:
                        _tty.setraw(fd)
                    except Exception:
                        pass
                elif ch == b'\x04':  # Ctrl+D → 失敗試行データ削除
                    try:
                        _termios.tcsetattr(fd, _termios.TCSADRAIN, old_settings)
                    except Exception:
                        pass
                    _do_purge_failed_trial(edge_ports, central_port)
                    try:
                        _tty.setraw(fd)
                    except Exception:
                        pass
                elif ch == b'\x0c':  # Ctrl+L → トポロジ表（ターミナル完結）
                    try:
                        _termios.tcsetattr(fd, _termios.TCSADRAIN, old_settings)
                    except Exception:
                        pass
                    _print_topology_dashboard(edge_ports, central_port)
                    try:
                        _tty.setraw(fd)
                    except Exception:
                        pass
                elif ch == b'\x09':  # Ctrl+I / Tab → 推論スナップショット
                    try:
                        _termios.tcsetattr(fd, _termios.TCSADRAIN, old_settings)
                    except Exception:
                        pass
                    _do_inference_snapshot()
                    try:
                        _tty.setraw(fd)
                    except Exception:
                        pass
                elif ch == b'\x03':  # Ctrl+C → SIGINT
                    try:
                        _termios.tcsetattr(fd, _termios.TCSADRAIN, old_settings)
                    except Exception:
                        pass
                    os.kill(os.getpid(), signal.SIGINT)
                    return
                elif ch == b'\x1c':  # Ctrl+\ → SIGQUIT
                    try:
                        _termios.tcsetattr(fd, _termios.TCSADRAIN, old_settings)
                    except Exception:
                        pass
                    os.kill(os.getpid(), signal.SIGQUIT)
                    return
        except Exception:
            pass
        finally:
            try:
                _termios.tcsetattr(fd, _termios.TCSADRAIN, old_settings)
            except Exception:
                pass

    t = threading.Thread(target=_listen, daemon=True, name="kbd-listener")
    t.start()
    return t, old_settings


# ------------------------------------------------------------------ #
# report コマンド（実験サマリを AI に生成させる）
# ------------------------------------------------------------------ #
async def show_report():
    print()
    print(_bold("─── AI 実験サマリ生成 ───"))

    provider = os.environ.get("AI_ADVISOR_PROVIDER", "").lower()
    api_key  = ""

    if not provider:
        # 対話式で設定を取得
        ai_cfg = select_ai_advisor()
        provider = ai_cfg.get("provider", "")
        api_key  = ai_cfg.get("api_key", "")
        if not provider:
            return
        # このプロセスの環境変数にセット（HFLAdvisor が参照できるように）
        os.environ["AI_ADVISOR_PROVIDER"] = provider
        if provider == "claude":
            os.environ["ANTHROPIC_API_KEY"] = api_key
        elif provider == "gemini":
            os.environ["GEMINI_API_KEY"] = api_key

    # プロジェクトルートを sys.path に追加して中央サーバモジュールを import
    sys.path.insert(0, str(ROOT))
    try:
        from central_server.config import STATE_DIR
        from central_server.ai_advisor import HFLAdvisor

        db_path = STATE_DIR / "training_metrics.db"
        if not db_path.exists():
            print(_yellow(f"メトリクス DB が見つかりません: {db_path}"))
            print(_gray("  実験を実行してからお試しください。"))
            print()
            return

        advisor = HFLAdvisor(
            db_path = db_path,
            log_dir = STATE_DIR / "ai_advisor_logs",
        )
        summary = await advisor.generate_experiment_summary()

        print()
        print(_bold("─── サマリ ───"))
        for line in summary.splitlines():
            print(f"  {line}")
        print()
        print(_gray(f"  ログ保存先: {STATE_DIR / 'ai_advisor_logs'}"))
        print()

    except ImportError as e:
        print(_red(f"モジュールの読み込みに失敗しました: {e}"))
        print(_gray("  プロジェクトルートから実行しているか確認してください。"))


# ------------------------------------------------------------------ #
# status コマンド
# ------------------------------------------------------------------ #
def show_status():
    print()
    print(_bold("─── サーバー死活確認 ───"))
    targets = [("中央サーバ", f"http://127.0.0.1:{CENTRAL_PORT}/healthz")]
    for i in range(4):  # 最大4台分チェック
        port = EDGE_BASE_PORT + i
        targets.append((f"エッジサーバ{i+1}", f"http://127.0.0.1:{port}/healthz"))

    any_up = False
    for label, url in targets:
        code = _get(url)
        if code == 0:
            status = _gray("● オフライン")
        elif code < 400:
            status = _green(f"● オンライン  (HTTP {code})")
            any_up = True
        else:
            status = _yellow(f"● 応答あり   (HTTP {code})")
            any_up = True
        print(f"  {label:<16} {status}  {_gray(url)}")

    if not any_up:
        print()
        print(_gray("  起動中のサーバが見つかりません。"))
        print(_gray("  python start.py で起動してください。"))
    print()


# ------------------------------------------------------------------ #
# test コマンド（test_runner.py に委譲）
# ------------------------------------------------------------------ #
def _run_test(args):
    """
    `python start.py test` から test_runner.py を呼び出す。

    モード選択:
      --mode A   仮想端末シミュレーション（サーバは別途起動済み前提）
      --mode B   ドライラン（サーバ不要・FL ロジック単体テスト）
      --mode C   E2E スモークテスト（サーバを自動起動・停止）
      --mode AC  フルシミュレーション（複数エッジ・複数端末・複数ラウンド）
    """
    MODES = ["A", "B", "C", "AC"]

    # モード未指定なら対話式で選択
    mode = args.mode.upper() if args.mode else ""
    if mode not in MODES:
        print()
        print(_bold("─── テストモードを選択 ───"))
        print("  A   仮想端末シミュレーション（サーバは起動済み前提）")
        print("  B   ドライラン（サーバ不要・FL ロジック単体テスト）")
        print("  C   E2E スモークテスト（サーバを自動起動・停止）")
        print("  AC  フルシミュレーション（複数エッジ・複数端末）")
        print()
        mode = _ask("モード", "C").upper()
        if mode not in MODES:
            print(_yellow("無効な選択です。C を使用します。"))
            mode = "C"

    # test_runner.py を subprocess で起動
    cmd = [sys.executable, str(ROOT / "test_runner.py"), mode]
    if args.edges > 0:
        cmd += ["--edges", str(args.edges)]
    if args.terms:
        cmd += ["--terms", str(args.terms)]
    if args.rounds:
        cmd += ["--rounds", str(args.rounds)]
    if args.quiet:
        cmd.append("--quiet")

    print(_gray(f"  実行: {' '.join(cmd)}"))
    result = subprocess.run(cmd, cwd=str(ROOT))
    sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="HFL Server Launcher", add_help=True)
    parser.add_argument("command", nargs="?", default="", help="status / report / test [A|B|C|AC]")
    parser.add_argument("--env",   default="", help="環境プロファイル名 (shinomilab/hachioji/osaka/localhost)")
    parser.add_argument(
        "--run-mode",
        default="",
        metavar="MODE",
        help="debug または production（省略時はターミナルで選択）。debug は既定でエッジ1・閾値1寄り",
    )
    parser.add_argument("--edges", type=int, default=0, help="エッジサーバの台数 (対話時の既定はメニュー)")
    parser.add_argument(
        "--central-threshold",
        type=int,
        default=None,
        help="中央集約に必要なエッジ数（未指定かつ環境変数なしのときはエッジ台数と同じ）",
    )
    parser.add_argument(
        "--edge-thresholds",
        default="",
        metavar="LIST",
        help="各エッジの端末集約閾値をカンマ区切り（例: 2,1）。未指定時は production で2台なら 2,1、debug では常に全1",
    )
    # test コマンド用オプション
    parser.add_argument("--mode",   default="", help="テストモード: A / B / C / AC")
    parser.add_argument("--terms",  type=int, default=2, help="テスト: エッジごとの仮想端末数")
    parser.add_argument("--rounds", type=int, default=3, help="テスト: ラウンド数")
    parser.add_argument("--quiet",  action="store_true", help="テスト: 詳細ログを抑制")
    args = parser.parse_args()

    if args.command == "status":
        show_status()
        return

    if args.command == "report":
        asyncio.run(show_report())
        return

    if args.command == "test":
        _run_test(args)
        return

    _ensure_uvicorn_for_launcher()

    print()
    print(_bold("╔══════════════════════════════╗"))
    print(_bold("║   HFL Server Launcher        ║"))
    print(_bold("╚══════════════════════════════╝"))

    run_mode = select_run_mode(args.run_mode)

    # 環境プロファイル選択
    if args.env:
        if args.env in PROFILES:
            profile_name, ip = args.env, PROFILES[args.env]["ip"]
        else:
            print(_yellow(f"プロファイル '{args.env}' が見つかりません。対話形式で選択します。"))
            profile_name, ip = select_profile()
    else:
        profile_name, ip = select_profile()

    ip = _apply_shinomilab_dynamic_ip(profile_name, ip)

    # エッジ台数
    if run_mode == "debug" and args.edges <= 0:
        n_edges = 1
        print()
        print(_gray("  （デバッグ … エッジ台数は 1。複数台にする場合は --edges N を付けてください）"))
    else:
        n_edges = args.edges if args.edges > 0 else select_edge_count()

    default_edge_thresholds = _compute_edge_thresholds(
        n_edges, args.edge_thresholds, run_mode=run_mode
    )
    default_central = 1  # 1台のエッジでも即時集約（全エッジ待ちにしたい場合は対話メニューで n_edges を入力）

    central_threshold, edge_thresholds = select_thresholds(
        n_edges,
        default_edge_thresholds,
        default_central,
        cli_central=args.central_threshold,
        cli_edge_str=args.edge_thresholds,
    )

    # AI アドバイザー設定
    ai_cfg = select_ai_advisor()

    start_servers(
        profile_name,
        ip,
        n_edges,
        ai_cfg=ai_cfg,
        central_threshold=central_threshold,
        edge_thresholds=edge_thresholds,
        run_mode=run_mode,
    )


if __name__ == "__main__":
    main()
