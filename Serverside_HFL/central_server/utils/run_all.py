
import argparse
import os
import sys
import time
import urllib.request
import urllib.error
import subprocess
from datetime import datetime
import threading
from pathlib import Path
import signal
import platform

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

CENTRAL_HOST = "0.0.0.0"
CENTRAL_PORT = 8000
CENTRAL_APP = "central_server.main:app"
CENTRAL_HEALTH = f"http://127.0.0.1:{CENTRAL_PORT}/healthz"

EDGE_HOST = "0.0.0.0"
EDGE_PORT = 8001
EDGE_APP = "edge_server.main:app"
EDGE_HEALTH = f"http://127.0.0.1:{EDGE_PORT}/healthz"
SEND_TO_DEVICE_URL = f"http://127.0.0.1:{EDGE_PORT}/send_to_device"  # optional probe

def wait_user_quit(stop_event: threading.Event):
    try:
        print("⏹ 停止するには Enter（または q+Enter）を押してください。")
        s = input().strip().lower()
        if s == "" or s == "q":
            stop_event.set()
    except EOFError:
        pass

def wait_for_http(url, timeout=45, interval=0.5, label=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    print(f"✅ {label or url} OK")
                    return True
                else:
                    print(f"…待機中 {label or url}: HTTP {r.status}")
        except urllib.error.HTTPError as e:
            print(f"…待機中 {label or url}: HTTPError {e.code}")
        except Exception as e:
            print(f"…待機中 {label or url}: {e.__class__.__name__}")
        time.sleep(interval)
    return False

def popen_uvicorn(module_app, host, port, env, log_file: Path, detach: bool = True):
    """
    uvicorn を起動するユーティリティ。
    - detach=True  : 親から切り離して起動（既存の運用）
    - detach=False : 親ターミナルの stdin を渡して起動（interactive 用）
    注意:
      - interactive 用は workers=1 を指定しています（stdin を共有するため）。
      - --reload は対話時に使わないでください（reload がサブプロセスを生成し stdin が渡らない場合があります）。
    """
    cmd = [
        sys.executable, "-m", "uvicorn", module_app,
        "--host", str(host), "--port", str(port),
        "--log-level", "info",
        "--workers", "1",
    ]
    stdout = log_file.open("w", encoding="utf-8", buffering=1)
    stderr = subprocess.STDOUT

    kwargs = dict(env=env, stdout=stdout, stderr=stderr)
    if platform.system() == "Windows":
        if detach:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["stdin"] = sys.stdin
    else:
        if detach:
            kwargs["start_new_session"] = True
        else:
            kwargs["stdin"] = sys.stdin

    print(f"🚀 起動: {module_app} ({host}:{port}) → {log_file} (detach={detach})")
    return subprocess.Popen([str(x) for x in cmd], **kwargs)

def terminate_tree(proc: subprocess.Popen, label=""):
    if not proc or proc.poll() is not None:
        return
    try:
        if platform.system() == "Windows":
            try:
                proc.send_signal(signal.CTRL_C_EVENT)
            except Exception:
                pass
            time.sleep(0.3)
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                except Exception:
                    pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGINT)
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
    except Exception as e:
        print(f"⚠ 終了処理({label})で例外: {e}")

def run_init_model():
    print("🔧 初期モデルを生成中…")
    res = subprocess.run([sys.executable, "create_torchscript_model.py"])
    if res.returncode != 0:
        raise SystemExit("初期モデル作成に失敗しました。")

def parse_args():
    p = argparse.ArgumentParser(description="中央＋エッジを一括起動")
    p.add_argument("--env", choices=["hachioji", "osaka"], help="環境プロファイル")
    p.add_argument("--central-health", default=CENTRAL_HEALTH)
    p.add_argument("--edge-health", default=EDGE_HEALTH)
    return p.parse_args()

def ensure_env(args):
    if "HFL_ENV" in os.environ:
        return os.environ["HFL_ENV"]
    if args.env:
        os.environ["HFL_ENV"] = args.env
        print(f"[ENV] 接続先プロファイル: {args.env}")
        return args.env
    try:
        choice = input("環境を選択してください (1: 八王子, 2: 大阪): ").strip()
    except EOFError:
        choice = "1"
    mapping = {"1": "hachioji", "2": "osaka"}
    env_name = mapping.get(choice, "hachioji")
    os.environ["HFL_ENV"] = env_name
    print(f"[ENV] 接続先プロファイル: {env_name}")
    return env_name

def main():
    args = parse_args()
    ensure_env(args)
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"

    # interactive をデフォルト ON にする（VS Code の実行ボタンでそのまま対話できる）
    # 非対話環境で無効にしたい場合は HFL_INTERACTIVE=0 をエクスポートしてください。
    interactive_mode = os.environ.get("HFL_INTERACTIVE", "1") != "0"
    if interactive_mode:
        print("[MODE] Interactive mode: ON (edge will inherit this terminal's stdin)")
    else:
        print("[MODE] Interactive mode: OFF (edge will be detached)")

    # 1) 初期モデル
    run_init_model()

    # ログファイル
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    central_log = LOG_DIR / f"central_{stamp}.log"
    edge_log = LOG_DIR / f"edge_{stamp}.log"

    central_proc = edge_proc = None
    stop_event = threading.Event()

    t = threading.Thread(target=wait_user_quit, args=(stop_event,), daemon=True)
    t.start()
    try:
        # 2) 中央サーバ（常に detach）
        central_proc = popen_uvicorn(CENTRAL_APP, CENTRAL_HOST, CENTRAL_PORT, child_env, central_log, detach=True)
        if not wait_for_http(args.central_health, timeout=60, label="中央サーバ"):
            print("❌ 中央サーバの起動確認に失敗。終了します。")
            terminate_tree(central_proc, "central")
            sys.exit(1)

        # 3) エッジサーバ（interactive_mode に応じて detach を切り替え）
        edge_proc = popen_uvicorn(EDGE_APP, EDGE_HOST, EDGE_PORT, child_env, edge_log, detach=(not interactive_mode))
        if not wait_for_http(args.edge_health, timeout=60, label="エッジサーバ"):
            print("⚠ エッジのヘルス確認に失敗。続行はしますが、端末が取得できない可能性があります。")

        # （任意）/send_to_device プローブ（失敗しても問題なし）
        try:
            with urllib.request.urlopen(SEND_TO_DEVICE_URL, timeout=3) as r:
                print(f"ℹ send_to_device probe: {r.status}")
        except Exception as e:
            print(f"ℹ send_to_device probe: {e.__class__.__name__}")

        # メインループ: Ctrl+C や Enter で停止
        while True:
            if stop_event.is_set():
                print("\n⛔ 要求により停止します…")
                break
            ret = central_proc.poll()
            if ret is not None:
                print(f"中央サーバが終了しました (code={ret})")
                break
            time.sleep(0.2)

    except KeyboardInterrupt:
        print("\n⛔ 中断されました。サーバを終了します…")
    finally:
        terminate_tree(edge_proc, "edge")
        terminate_tree(central_proc, "central")
        for p in (edge_proc, central_proc):
            if p is not None:
                try:
                    p.wait(timeout=2)
                except Exception:
                    pass
        print("🧹 終了処理完了")

if __name__ == "__main__":
    main()
