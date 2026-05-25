"""
シンプルな端末シミュレータ: ローカル学習5回分を連続でエッジに送信する。
- エッジは /receive_terminal_weights/{terminal_id} を受ける
- 送信は f32_flat (raw float32 little-endian) を使い、input_size=1, hidden_size=0, output_size=1 の場合は 2 要素 (weight, bias)

実行: python scripts/sim_terminal_upload.py

注意: サーバが localhost:8001 で起動していることを確認してください。
"""

import httpx
import struct
import time

EDGE_URL = "http://127.0.0.1:8001"
TERMINAL_ID = "sim-term-01"
MODEL_ID = "sim-model"
BASE_HASH = "basehash-sim"

# 二つの float32 を little-endian で作る（weight=0.1, bias=0.2）
vals = struct.pack('<2f', 0.1, 0.2)

# 5 回、ラウンド 1..5 を送信
with httpx.Client(timeout=10.0) as client:
    for r in range(1, 6):
        url = f"{EDGE_URL}/receive_terminal_weights/{TERMINAL_ID}"
        files = {
            'weights': ('weights.bin', vals, 'application/octet-stream')
        }
        data = {
            'round_id': str(r),
            'model_id': MODEL_ID,
            'base_hash': BASE_HASH,
            'n_samples': '10',
            'payload_kind': 'full',
            'dtype': 'f32_flat',
            'input_size': '1',
            'hidden_size': '0',
            'output_size': '1'
        }
        print(f"-> Sending round={r} ... ")
        try:
            resp = client.post(url, data=data, files=files)
            print("  status:", resp.status_code)
            try:
                print("  json:", resp.json())
            except Exception:
                print("  text:", resp.text[:500])
        except Exception as e:
            print("  request failed:", e)
        time.sleep(0.8)

print("done")
