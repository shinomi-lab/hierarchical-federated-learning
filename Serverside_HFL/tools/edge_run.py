import os
import sys
import time

def ask(prompt: str, default: str | None = None) -> str:
    try:
        v = input(f"{prompt}{' ['+default+']' if default else ''}: ").strip()
        return v or (default or "")
    except Exception:
        return default or ""

def main():
    print("=== Edge Server Interactive Launcher ===")
    print("1) ポートを指定して起動 (例: 8001)")
    print("2) 既定ポートで起動 (8001)")
    print("3) 終了")
    choice = ask("選択してください", "1")

    if choice == "3":
        print("終了します。")
        return

    port = "8001"
    if choice == "1":
        port = ask("使用するポート番号", port)

    edge_id = os.getenv("EDGE_SERVER_ID", f"edge-server-{port}")
    edge_url = os.getenv("EDGE_URL", f"http://127.0.0.1:{port}")
    central_url = os.getenv("CENTRAL_SERVER_URL", "http://127.0.0.1:8000")

    # 反映
    os.environ["EDGE_SERVER_ID"] = edge_id
    os.environ["EDGE_URL"] = edge_url
    os.environ["CENTRAL_SERVER_URL"] = central_url

    print("\n[設定]")
    print(f"  EDGE_SERVER_ID = {edge_id}")
    print(f"  EDGE_URL       = {edge_url}")
    print(f"  CENTRAL_SERVER_URL = {central_url}")
    print(f"  PORT           = {port}")

    print("\n起動します... (Ctrl+Cで停止)")
    time.sleep(0.5)

    # uvicorn 実行
    try:
        import uvicorn
        uvicorn.run("edge_server.main:app", host="0.0.0.0", port=int(port))
    except Exception as e:
        print(f"起動に失敗しました: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
