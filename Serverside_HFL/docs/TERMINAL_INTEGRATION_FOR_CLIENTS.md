# 端末（クライアント）向け送信ガイド

この文書は、端末開発者向けに「集約済み重み（または訓練メタデータ）を中央/エッジサーバへ送信する際の必須要件・推奨実装・テスト手順」をまとめたものです。

目的
- 重複送信（端末側からの同一ラウンド再送）を最小化する
- サーバ側の first-wins (idempotency) に合わせた安全な送信フローを提供する
- 運用で原因追跡ができるログを残す
- ネットワーク障害や端末再起動に強い送信クライアントを実装する

概要（簡潔）
- 送信前に content SHA（sha256:...）を計算し metadata に含める
- 各送信に一意な `req_id`（UUID）を付与する
- サーバの軽量マーカーAPI（/api/markers）で事前確認して送信をスキップできる
- サーバから `ack` を受け取ったらローカルに永続化して再送を防止する
- 再送はネットワークエラー/タイムアウト/5xx のみ、指数バックオフを適用する

必須実装要件
1) 一意リクエストID
- 送信ごとに UUIDv4 などで生成し `req_id` として送る。サーバのログと突き合わせるために必須。

2) コンテンツ SHA
- 送信するファイル（.pt 等）について SHA-256 を計算し `content_sha256`（例: `sha256:abcdef...`）として送る。

3) 送信結果の永続化
- サーバ応答で `ack:true` を受け取ったら、ローカルDB（sqlite 等）に `sha, req_id, round, ack_timestamp` を保存する。再起動後も参照して同一 SHA を再送しない。

4) 再送ポリシー
- 再送はネットワーク例外／タイムアウト／5xx の場合にのみ行う。クライアントエラー(4xx)は再送しない。
- 指数バックオフ例: 1s, 2s, 4s, 8s, 16s（最大 5 回）。合計再送期間は用途に応じて制限（例 60–120 秒）。

5) タイムアウト
- HTTP タイムアウトを設定（例 30 秒）。ブロッキングを避ける。

6) ロギング
- 送信試行 (req_id, sha, round, timestamp)、HTTP ステータス、レスポンスボディを必ずログに残す。

推奨実装（運用性向上）
- 事前チェック: サーバの `GET /api/markers/hash/{sha}` を呼び、既に受け付けられていれば送信をスキップしてローカルに `ack` を書き込む。
- 送信理由の付与: 再送時に `retry_reason` を metadata に含める（例：network_error, process_restart）。
- 送信済み確認 API: 送信前に `GET /api/markers/edge-round?round=...&edge_id=...` を使って、エッジ単位の重複をチェックする（エッジ→中央直接はエッジ側で確認する設計の場合）。

API 仕様（端末が使う想定）
- POST /edge_update
  - multipart/form-data
  - fields:
    - weights: file (binary)
    - edge_id: string
    - round: int
    - num_clients: int
    - sum_n_samples: int
    - content_sha256: string
    - run_id (または req_id): string
  - 期待する成功レスポンス:
    - {"status":"ok", "ack": true, "ack_timestamp": "20251105_..."}
  - 重複レスポンス:
    - {"status":"duplicate_ignored", "ack": true, "ack_timestamp": "..."}

- GET /api/markers/hash/{sha}
  - 返却: {"exists": true|false}

- GET /api/markers/edge-round?round=...&edge_id=...
  - 返却: {"exists": true|false}

サンプル: Python 送信フロー（簡易）
```python
import requests, uuid, time, hashlib

def sha256_of_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return 'sha256:' + h.hexdigest()

def send_weights(server_url, file_path, edge_id, round, num_clients, sum_n_samples):
    sha = sha256_of_file(file_path)
    req_id = str(uuid.uuid4())
    # Optional: check server marker
    try:
        resp = requests.get(f"{server_url.rstrip('/')}/api/markers/hash/{sha}", timeout=3)
        if resp.status_code == 200 and resp.json().get('exists'):
            # mark local ack and return
            return True
    except Exception:
        pass

    files = {'weights': open(file_path,'rb')}
    data = {
        'edge_id': edge_id,
        'round': str(round),
        'num_clients': str(num_clients),
        'sum_n_samples': str(sum_n_samples),
        'content_sha256': sha,
        'run_id': req_id,
    }
    # exponential backoff
    backoff = 1
    for attempt in range(5):
        try:
            r = requests.post(server_url, data=data, files=files, timeout=30)
            if r.status_code == 200:
                j = r.json()
                if j.get('ack'):
                    # persist local ack
                    return True
                else:
                    return False
            elif 500 <= r.status_code < 600:
                time.sleep(backoff)
                backoff = min(backoff*2, 30)
                continue
            else:
                # client error -> do not retry
                return False
        except requests.RequestException:
            time.sleep(backoff)
            backoff = min(backoff*2, 30)
            continue
    return False
```

テストケース（端末側で実行）
1. 正常送信: 送信して ack を受け取る。ローカル DB に保存され、再送されないことを確認。
2. 再送ケース: 同一ファイルを意図的に2回送信して 2 回目が `duplicate_ignored` を受け取ることを確認。
3. ネットワーク障害: 送信中にタイムアウト → 再送が行われ成功するまで backoff が働くことを確認。
4. 再起動ケース: 送信中に端末再起動 → local DB により未送信分が復旧・再送される。

運用上の注意
- TLS を必須にする（HTTPS）。通信は常に暗号化すること。
- req_id と content_sha を端末ログに残す（追跡のため）。
- 送信済みのローカル DB は定期的に古いレコードを TTL で掃除する（端末容量に応じて）。

付録: 端末向けのチェックリスト
- [ ] req_id を生成している
- [ ] content_sha256 を計算・送信している
- [ ] ack を永続化する仕組みがある（sqlite 等）
- [ ] 再送は指数バックオフで限定的に行う
- [ ] サーバの marker API (optional) を活用している
- [ ] 送信ログに req_id / sha / timestamp / status を記録している

参考実装
- リポジトリ内の `scripts/terminal_client.py` を参照してください。これが端末実装の参考コードです。

---
この文書を端末開発者に配布する際、API のエンドポイント（ホスト名／パス）とフィールド名が実際のデプロイと一致しているかを確認してください。実際に配布する版を希望する形式（Markdown/PDF/テキスト）を教えてください。

## 実践的なサンプル: 端末クライアント（完全版）

以下は、端末側でそのまま貼り付けて使える実践的な Python のクライアント実装例です。
特徴:
- content SHA を計算して送信
- サーバのマーカー API を事前に確認して送信をスキップ
- ack をローカル sqlite に永続化して再送を防止
- 再送はネットワーク例外/タイムアウト/5xx のみで指数バックオフ

注意: このサンプルは参考実装です。実際のエンドポイント (HOST/パス) や証明書検証、タイムアウト等は運用に合わせて調整してください。

```python
#!/usr/bin/env python3
"""terminal_client_sample.py

使い方 (例):
  python terminal_client_sample.py --server https://central.example.com --file /path/to/weights.pt --edge_id edge-1 --round 42

"""
import argparse
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

import requests


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS acks (
    content_sha TEXT PRIMARY KEY,
    req_id TEXT,
    edge_id TEXT,
    round INTEGER,
    ack_timestamp TEXT,
    saved_at INTEGER
);
"""


def init_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute(DB_SCHEMA)
    conn.commit()
    return conn


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def has_local_ack(conn: sqlite3.Connection, sha: str) -> bool:
    cur = conn.execute("SELECT 1 FROM acks WHERE content_sha = ?", (sha,))
    return cur.fetchone() is not None


def save_local_ack(conn: sqlite3.Connection, sha: str, req_id: str, edge_id: str, round_num: int, ack_ts: str):
    conn.execute(
        "INSERT OR REPLACE INTO acks(content_sha, req_id, edge_id, round, ack_timestamp, saved_at) VALUES(?,?,?,?,?,?)",
        (sha, req_id, edge_id, round_num, ack_ts, int(time.time())),
    )
    conn.commit()


def check_server_marker(server: str, sha: str, timeout: float = 3.0) -> bool:
    url = server.rstrip("/") + f"/api/markers/hash/{sha}"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return bool(r.json().get("exists"))
    except requests.RequestException:
        # サーバ確認が失敗しても送信を中止しない（保守的に送信する場合はここで False を返す）
        return False
    return False


def post_weights_with_backoff(server: str, file_path: Path, edge_id: str, round_num: int, num_clients: int, sum_n_samples: int, conn: sqlite3.Connection):
    sha = sha256_of_file(file_path)
    if has_local_ack(conn, sha):
        print("[skip] local ack exists for", sha)
        return True

    # Optional: 事前確認で送信をスキップ
    if check_server_marker(server, sha):
        print("[skip] server already has sha", sha)
        # サーバが既に持っているならローカル ack を書く（ack_timestamp は不明なので現在時刻を入れる）
        save_local_ack(conn, sha, "precheck", edge_id, round_num, time.strftime("%Y%m%d_%H%M%S"))
        return True

    req_id = str(uuid.uuid4())
    files = {"weights": open(file_path, "rb")}
    data = {
        "edge_id": edge_id,
        "round": str(round_num),
        "num_clients": str(num_clients),
        "sum_n_samples": str(sum_n_samples),
        "content_sha256": sha,
        "run_id": req_id,
    }

    backoff = 1
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            url = server.rstrip("/") + "/edge_update"
            r = requests.post(url, data=data, files=files, timeout=30)
            # 200 系で ack を受け取ったらローカルに保存
            if r.status_code == 200:
                j = r.json()
                if j.get("ack"):
                    ack_ts = j.get("ack_timestamp") or time.strftime("%Y%m%d_%H%M%S")
                    save_local_ack(conn, sha, req_id, edge_id, round_num, ack_ts)
                    print("[ok] sent", sha, "ack_timestamp=", ack_ts)
                    return True
                else:
                    print("[error] server returned no ack", r.text)
                    return False
            elif 500 <= r.status_code < 600:
                # サーバ側エラー -> 再試行
                print(f"[retry] server error {r.status_code}, attempt {attempt}/{max_attempts}")
            else:
                # 4xx 等は再送しない
                print(f"[fail] client error {r.status_code}: {r.text}")
                return False
        except requests.RequestException as e:
            print(f"[retry] network error on attempt {attempt}: {e}")

        # Backoff
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

    print("[fail] exceeded retry attempts")
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--edge_id", required=True)
    p.add_argument("--round", required=True, type=int)
    p.add_argument("--num_clients", default=1, type=int)
    p.add_argument("--sum_n_samples", default=0, type=int)
    p.add_argument("--db", default="./.terminal/acks.sqlite")
    args = p.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        raise SystemExit("file not found: " + str(file_path))

    conn = init_db(Path(args.db))
    ok = post_weights_with_backoff(args.server, file_path, args.edge_id, args.round, args.num_clients, args.sum_n_samples, conn)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
```

### このサンプルのポイントと導入手順

- DB: `./.terminal/acks.sqlite` に ack を入れることで再起動後も重複送信を避けられます。運用では TTL（古い ack の掃除）を追加してください。
- サーバ確認: `GET /api/markers/hash/{sha}` はネットワークが不安定なら失敗することがあります。事前確認が失敗しても送信を試みる実装にしておくのが安全です（上のサンプルの挙動）。
- 実行例:

```powershell
# PowerShell 上での実行例
python terminal_client_sample.py --server https://central.example.com --file .\weights.pt --edge_id edge-1 --round 42
```

---

必要なら、このサンプルを `scripts/terminal_client.py` としてリポジトリに追加しておきますが、まずはこのドキュメント内の例で問題ないか教えてください。
