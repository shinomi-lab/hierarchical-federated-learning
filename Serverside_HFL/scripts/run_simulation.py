"""
run_simulation.py

簡易スクリプト: ローカル環境で中央サーバとエッジサーバを順に起動し、基本的なヘルスチェックを行います。

注意: このスクリプトはローカルの Python 環境で実行してください。uvicorn がインストールされていることが前提です。
"""
import subprocess
import sys
import time
import signal
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
CENTRAL_HOST = "127.0.0.1"
CENTRAL_PORT = 8000
EDGE_HOST = "127.0.0.1"
EDGE_PORT = 8001

uvicorn_cmd = sys.executable.replace('python.exe', 'Scripts\\uvicorn.exe') if sys.platform.startswith('win') else 'uvicorn'

def start_uvicorn(module_app, host, port):
    cmd = [sys.executable, "-m", "uvicorn", f"{module_app}", "--host", host, "--port", str(port), "--log-level", "info"]
    print("Starting:", " ".join(cmd))
    return subprocess.Popen(cmd, cwd=str(ROOT))


def wait_for(url, timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = httpx.get(url, timeout=2)
            if r.status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    central_proc = start_uvicorn('central_server.main:app', CENTRAL_HOST, CENTRAL_PORT)
    try:
        if not wait_for(f'http://{CENTRAL_HOST}:{CENTRAL_PORT}/docs'):
            print('Central did not become ready in time')
            central_proc.terminate()
            central_proc.wait()
            return
        print('Central ready')

        edge_proc = start_uvicorn('edge_server.main:app', EDGE_HOST, EDGE_PORT)
        if not wait_for(f'http://{EDGE_HOST}:{EDGE_PORT}/docs'):
            print('Edge did not become ready in time')
            edge_proc.terminate()
            edge_proc.wait()
            central_proc.terminate()
            central_proc.wait()
            return
        print('Edge ready')

        # Basic smoke checks
        try:
            r = httpx.get(f'http://{CENTRAL_HOST}:{CENTRAL_PORT}/')
            print('Central root:', r.status_code)
        except Exception as e:
            print('Central root error', e)

        try:
            r = httpx.get(f'http://{EDGE_HOST}:{EDGE_PORT}/')
            print('Edge root:', r.status_code)
        except Exception as e:
            print('Edge root error', e)

        print('Servers started. Press Ctrl+C to stop both.')
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('Interrupted')
    finally:
        for p in (locals().get('edge_proc'), locals().get('central_proc')):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()

if __name__ == '__main__':
    main()
