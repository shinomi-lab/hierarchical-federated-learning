# サーバ側改善ドキュメント — client_meta の受信・保存・可視化

作成日: 2025-11-04
作成者: （自動生成ドキュメント）

目的
- モバイル端末から送信されるローカル学習時間（各エポック所要時間、合計時間など、以下まとめて "client_meta"）をサーバ側で受け取り、保存・集計・可視化できるようにする。
- これにより、端末側の計算負荷とネットワーク待ち（429 等の再試行による待機時間）の比率を定量化し、ボトルネックの特定と対策を進めやすくする。

要件（高レベル）
1. 既存の重みアップロード API（例: `POST /receive_terminal_weights/{terminal_id}`）で、multipart の text 部分に付与される `client_meta` フィールド（JSON 文字列）を受け取る。
2. `client_meta` を検証・パースし、必要なメトリクス（`total_local_ms`, `epoch_durations_ms`, `timestamp_ms`, `terminal_id` 等）を DB に保存する。
3. 保存したメトリクスにアクセスする簡易 API（例: `/metrics/latest`, `/metrics/query`）を用意し、運用者が端末ごとや時間帯で集計できるようにする。
4. サーバはクライアント実装で期待されるレスポンス形式を返す（リトライに対応するため `429` とともに JSON の `wait_seconds` を返す等）。
5. 受信の安定化のため、サイズ制限・最大配列長・バリデーションを設ける。

クライアント（端末）側の前提（既に実装済）
- 端末は `client_meta` を multipart の text part 名 `client_meta` に JSON 文字列として入れて送信する。
- `client_meta` の例:
```json
{
  "terminal_id": "device-001",
  "total_local_ms": 12345,
  "epoch_durations_ms": [250, 240, 230, 260, 254],
  "timestamp_ms": 1700000000000
}
```

API 変更の詳細（サーバ側）
- 受信エンドポイント: 既存の `POST /receive_terminal_weights/{terminal_id}` の multipart フォーム処理に下記を追加する。
  - 追加フィールド: `client_meta` (optional, text)
  - 挙動:
    - `client_meta` が存在する場合、JSON を parse し検証する。
    - 不正 JSON の場合は 400 を返す（ただし重み処理は継続できるならログを残してスキップする選択肢あり）。
    - 正常なら DB に保存し、`{"status":"ok"}` を返す（既存レスポンスに合わせる）。

client_meta の JSON スキーマ（推奨）
- terminal_id: string (必須/ただし URL の path parameter と同一であることを期待)
- total_local_ms: integer (必須)
- epoch_durations_ms: array[integer] (任意だが可能なら小さめに制限)
- timestamp_ms: integer (送信時刻または計測時刻)
- optional: cpu_usage_pct, battery_level_pct, network_type 等（将来拡張）

DB 例（SQLite / Postgres 互換の簡易スキーマ）
```sql
CREATE TABLE client_metrics (
  id SERIAL PRIMARY KEY,
  terminal_id TEXT NOT NULL,
  total_local_ms INTEGER NOT NULL,
  epoch_durations_json TEXT,
  timestamp_ms BIGINT,
  received_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE INDEX idx_client_metrics_terminal_ts ON client_metrics(terminal_id, timestamp_ms);
```

FastAPI サンプル（受け取り・保存）
```python
# server_metrics_sample.py
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
import json
import sqlite3
from datetime import datetime

app = FastAPI()
DB = 'metrics.db'

def init_db():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS client_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            terminal_id TEXT,
            total_local_ms INTEGER,
            epoch_durations TEXT,
            timestamp_ms INTEGER,
            received_at TEXT
        )
    ''')
    con.commit()
    con.close()

init_db()

@app.post('/receive_terminal_weights/{terminal_id}')
async def receive_weights(terminal_id: str,
                          round_id: int = Form(...),
                          model_id: str = Form(...),
                          client_meta: str | None = Form(None),
                          weights: UploadFile = File(...)):
    # ここで重みファイルの処理を行う（保存・検証など）
    if client_meta:
        try:
            j = json.loads(client_meta)
        except Exception as e:
            # クライアントが送った JSON が不正
            raise HTTPException(status_code=400, detail=f"invalid client_meta: {e}")

        total_local_ms = j.get('total_local_ms')
        epoch_durations = j.get('epoch_durations_ms')
        timestamp_ms = j.get('timestamp_ms')

        con = sqlite3.connect(DB)
        cur = con.cursor()
        cur.execute(
            'INSERT INTO client_metrics (terminal_id, total_local_ms, epoch_durations, timestamp_ms, received_at) VALUES (?, ?, ?, ?, ?)',
            (terminal_id, total_local_ms, json.dumps(epoch_durations), timestamp_ms, datetime.utcnow().isoformat())
        )
        con.commit(); con.close()
    # 既存の重み処理の続行
    return {"status":"ok"}

@app.get('/metrics/latest')
async def metrics_latest(limit: int = 100):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute('SELECT id, terminal_id, total_local_ms, epoch_durations, timestamp_ms, received_at FROM client_metrics ORDER BY id DESC LIMIT ?', (limit,))
    rows = cur.fetchall(); con.close()
    return [{
        'id': r[0], 'terminal_id': r[1], 'total_local_ms': r[2], 'epoch_durations': json.loads(r[3]) if r[3] else None, 'timestamp_ms': r[4], 'received_at': r[5]
    } for r in rows]
```

API レスポンスの一貫性（429 の扱い）
- クライアント側は `retrying()` 内で `Retry-After` ヘッダや JSON の `wait_seconds` を参照しているため、サーバ側は 429 を返す際に下記のどちらかを返すことを推奨します。
  - `HTTP 429` + `Retry-After: <seconds>` ヘッダ（秒数）
  - または `HTTP 429` + `Content-Type: application/json` ボディに `{"error":"rate_limit_exceeded","message":"Too many requests","wait_seconds":1.5}` のような JSON を返す
- こうすることでクライアントは server-provided wait を尊重して再試行タイミングを決められる。

バリデーション & 安全対策
- `client_meta` の JSON のサイズに上限を設ける（例: 8KB）
- `epoch_durations_ms` の長さに上限（例: 100 要素）
- DB に保存する前に型チェックを行う（整数性、非負）
- ログには PII を出さない（端末ID が許容範囲であるか慎重に扱う）

レート制御 / バックプレッシャ
- サーバ側で受信頻度が増えた場合は 429 を返す（上記の `wait_seconds` を含める）。
- クライアントは既に `NetworkClient.retrying()` で 429 を扱う実装があるので、サーバは `wait_seconds` を合理的に設定すること。

保存後の可視化案
- 最小構成: SQLite → CSV export → Excel/Google Sheets
- 推奨: InfluxDB / Prometheus + Grafana で可視化
  - 端末ID ごとの `total_local_ms`、時間帯別の平均、95th percentile、epochごとの分布 をグラフ化
- 分析用クエリ例（Postgres の場合）:
```sql
-- 端末 device-001 の直近 24 時間の平均 local time
SELECT date_trunc('hour', to_timestamp(received_at)::timestamp) as hour, avg(total_local_ms)
FROM client_metrics
WHERE terminal_id = 'device-001' AND received_at > now() - interval '24 hours'
GROUP BY hour ORDER BY hour;
```

テスト & QA
- 単体テスト: `client_meta` が正しくパースされ DB に書き込まれることを確認するユニットテストを追加する。
- 結合テスト: モバイル（或いは curl）から multipart リクエストを送り、`/metrics/latest` で確認する。
- curl テスト例:
```bash
curl -v -F "round_id=12" -F "model_id=myModel" -F "client_meta={\"terminal_id\":\"device-001\",\"total_local_ms\":1234,\"epoch_durations_ms\":[250,250],\"timestamp_ms\":1700000000000}" -F "weights=@./weights.bin;type=application/octet-stream" http://<edge-host>:8001/receive_terminal_weights/device-001
```

移行手順（簡易）
1. ブランチを切る: `feature/accept-client-meta`。
2. 既存 receive endpoint の multipart handler に `client_meta` 処理を追加。
3. DB migration を追加（Postgres なら SQL マイグレーションファイル）。
4. ユニットテスト・結合テストを追加し CI を通す。
5. ステージング環境でクライアント（端末）とテスト送信を実施。
6. 本番ロールアウト（負荷を見ながら段階的に）。

PR テンプレート（推奨）
```
タイトル: サーバ: client_meta 受け取り・保存の追加

説明:
- 何を追加したか / 変更したか
- DB migration を含むか
- 互換性の注意点

テスト:
- ユニットテストの項目
- 結合テストの手順（curl コマンドなど）

デプロイ手順:
- ステージングでの手順
- 本番ロールアウト時の注意点（レート制御の閾値等）
```

運用的注意点
- 大量端末からの送信を想定する場合、DB 書き込みレート・保管容量を定期的に監視する。古いデータはローテーションでアーカイブする。 
- privacy: 端末 ID を直接ユーザーに紐づけるような情報は避け、必要ならハッシュ化して保存する。

次のステップ（推奨）
- すぐに着手: 上記 FastAPI サンプルを使った PoC を作成し、端末からの実データ送信で動作確認を行う。
- 併行して: 受け取りデータの可視化パイプライン（InfluxDB/Grafana）を立てる。
- 長期: 端末側で送信頻度のサンプリングを実装（全端末常時送信は避ける）。

必要なら私が次に行います:
- FastAPI サンプルリポジトリを用意して README と docker-compose を作成する
- Postgres 用の migration SQL を作る
- CI テスト（pytest）や curl ベースの統合テストを追加する

---
以上です。サーバ実装でどれを進めるか教えてください。希望があれば `feature/accept-client-meta` ブランチ用の PR テンプレート文やコミットメッセージも作ります。
