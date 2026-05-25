#!/usr/bin/env python3
"""
Simple Flask server to accept /upload_client_logs/{terminal_id} for local testing.
Saves uploaded files under uploads/{terminal_id}/{sha256}_{filename} and returns JSON ack.
"""
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
import os
import hashlib
from datetime import datetime

app = Flask(__name__)
UPLOAD_DIR = os.path.join(os.getcwd(), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_BYTES = 50 * 1024 * 1024  # 50MB overall limit

@app.route('/upload_client_logs/<terminal_id>', methods=['POST'])
def upload_client_logs(terminal_id):
    # Basic auth check: optional Bearer token in Authorization header
    auth = request.headers.get('Authorization')
    if auth is None:
        return jsonify({'ack': False, 'status': 'unauthorized', 'detail': 'Missing Authorization header'}), 401

    files = []
    for key in request.files:
        f = request.files.get(key)
        if f:
            filename = secure_filename(f.filename or key)
            data = f.read()
            if len(data) == 0:
                continue
            if len(data) > MAX_BYTES:
                return jsonify({'ack': False, 'status': 'payload_too_large', 'detail': f'File {filename} too large'}), 413
            sha = hashlib.sha256(data).hexdigest()
            dest_dir = os.path.join(UPLOAD_DIR, terminal_id)
            os.makedirs(dest_dir, exist_ok=True)
            dest_path = os.path.join(dest_dir, f"{sha}_{filename}")
            if os.path.exists(dest_path):
                status = 'duplicate_ignored'
            else:
                with open(dest_path, 'wb') as out:
                    out.write(data)
                status = 'stored'
            files.append({'name': filename, 'size': len(data), 'sha256': sha, 'status': status})

    meta = request.form.get('meta') or (request.files.get('meta').read().decode('utf-8') if request.files.get('meta') else None)

    return jsonify({'ack': True, 'status': 'stored', 'received': files, 'meta': meta})

@app.route('/upload_client_logs/<terminal_id>/<sha256>', methods=['GET'])
def check_exists(terminal_id, sha256):
    dirp = os.path.join(UPLOAD_DIR, terminal_id)
    if not os.path.isdir(dirp):
        return jsonify({'exists': False})
    for fn in os.listdir(dirp):
        if fn.startswith(sha256):
            return jsonify({'exists': True})
    return jsonify({'exists': False})

@app.route('/')
def index():
    return jsonify({'ok': True, 'time': datetime.utcnow().isoformat()})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8001, debug=True)

