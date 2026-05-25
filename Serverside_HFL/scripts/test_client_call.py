"""
Run FastAPI TestClient against the edge app in-process to simulate 5 terminal uploads.
This avoids needing to run uvicorn and works inside the repository.
"""
import sys
from pathlib import Path

# Ensure repo root is on sys.path so `edge_server` package can be imported
sys.path.append(str(Path(__file__).resolve().parent.parent))
# Also add the edge_server package dir so modules that import top-level names like
# `from startup import ...` succeed when modules assume edge_server/ is on sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent / "edge_server"))

from fastapi.testclient import TestClient
from edge_server.main import app
import struct
import time
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# For this in-process test we run a tiny HTTP server to act as central's /edge_update
class _SimpleCentralHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Accept any POST to /edge_update and respond 200 with JSON
        length = int(self.headers.get('content-length', 0))
        _ = self.rfile.read(length)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

def _start_fake_central(port=9000):
    server = HTTPServer(('127.0.0.1', port), _SimpleCentralHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# Ensure edge's config will use our local central (fake server)
os.environ['CENTRAL_SERVER_URL'] = 'http://127.0.0.1:9000'

# start fake central server on port 9000
_central = _start_fake_central(9000)

client = TestClient(app)
TERMINAL_ID = 'sim-term-01'
EDGE_URL = ''  # not used for TestClient

vals = struct.pack('<2f', 0.1, 0.2)

for r in range(1,6):
    url = f"/receive_terminal_weights/{TERMINAL_ID}"
    files = {'weights': ('weights.bin', vals, 'application/octet-stream')}
    data = {
        'round_id': str(r),
        'model_id': 'sim-model',
        'base_hash': 'basehash-sim',
        'n_samples': '10',
        'payload_kind': 'full',
        'dtype': 'f32_flat',
        'input_size': '1',
        'hidden_size': '0',
        'output_size': '1'
    }
    print(f"POST round={r} ->", end=' ')
    resp = client.post(url, data=data, files=files)
    print(resp.status_code)
    try:
        print(resp.json())
    except Exception:
        print(resp.text)
    time.sleep(0.2)

print('done')
