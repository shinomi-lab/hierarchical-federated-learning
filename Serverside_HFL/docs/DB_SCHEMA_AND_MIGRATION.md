# 分析向けDB設計と移行手順（SQLite 用 POC）

目的
- 学習結果・端末メタ・サーバ側イベント・配布ログを一元化し、研究・解析に使える構造化データベースを提供する。
- 単一ノード運用を想定してまず SQLite（将来 Postgres へ移行可能）で POC を作る。

設計方針（要点）
- モデルのバイナリはファイルシステムに置き、DB はパス・ハッシュ・メタのみを保持する。
- `run_id` を第一級オブジェクトとし、ラウンドは `run_id` 名前空間の中で解釈する（ラウンド1へ戻す運用に対応）。
- トランザクションは短く。WAL + `synchronous=FULL` を推奨。

推奨 SQLite PRAGMA
```
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
```

主要テーブル（DDL: SQLite）
```
BEGIN TRANSACTION;
-- experiments/run 管理
CREATE TABLE IF NOT EXISTS experiments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  description TEXT,
  params JSON,
  created_at REAL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY,
  experiment_id INTEGER REFERENCES experiments(id),
  start_ts REAL,
  end_ts REAL,
  operator TEXT,
  notes TEXT
);

-- 端末・エッジ管理
CREATE TABLE IF NOT EXISTS edges(edge_id TEXT PRIMARY KEY, url TEXT, last_seen REAL, meta JSON);
CREATE TABLE IF NOT EXISTS terminals(terminal_id TEXT PRIMARY KEY, meta JSON, last_seen REAL);

-- 端末アップデート（端末→エッジ）
CREATE TABLE IF NOT EXISTS terminal_updates(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  terminal_id TEXT REFERENCES terminals(terminal_id),
  run_id TEXT REFERENCES runs(run_id),
  round INT,
  seq INT,
  n_samples INT,
  sum_n_samples INT,
  sha TEXT,
  path TEXT,
  event_ts REAL,
  meta JSON,
  created_at REAL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_updates_run_round ON terminal_updates(run_id, round);
CREATE INDEX IF NOT EXISTS idx_updates_terminal_time ON terminal_updates(terminal_id, event_ts);

-- エッジ内集約
CREATE TABLE IF NOT EXISTS aggregations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  round INT,
  edge_id TEXT,
  filename TEXT,
  model_hash TEXT,
  num_clients INT,
  sum_n_samples INT,
  duration_ms INT,
  saved_path TEXT,
  saved_at REAL DEFAULT (strftime('%s','now'))
);

-- 中央でのモデルバッチ
CREATE TABLE IF NOT EXISTS models(
  model_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  round INT,
  batch_name TEXT,
  file_path TEXT,
  hash TEXT,
  created_at REAL,
  promoted_at REAL,
  promoted_to_canonical INT DEFAULT 0,
  meta JSON
);

-- 中央→エッジ配布ログ
CREATE TABLE IF NOT EXISTS pushes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id INTEGER REFERENCES models(model_id),
  edge_id TEXT,
  attempt INT,
  status TEXT,
  http_status INT,
  elapsed_s REAL,
  resp_text_trunc TEXT,
  ts REAL DEFAULT (strftime('%s','now'))
);

-- pending / duplicate 防止
CREATE TABLE IF NOT EXISTS pending(
  filename TEXT PRIMARY KEY,
  run_id TEXT,
  round INT,
  meta JSON,
  saved_at REAL
);

CREATE TABLE IF NOT EXISTS sent_rounds(
  key TEXT PRIMARY KEY, -- e.g. '{round}:{run_id}'
  round INT,
  run_id TEXT,
  added_at REAL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS in_progress(
  key TEXT PRIMARY KEY,
  round INT,
  run_id TEXT,
  updated_at REAL DEFAULT (strftime('%s','now'))
);

-- 汎用イベント / メトリクス
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT,
  event_type TEXT,
  payload JSON,
  ts REAL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS metrics(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT,
  entity_id TEXT,
  metric_name TEXT,
  value REAL,
  meta JSON,
  ts REAL DEFAULT (strftime('%s','now'))
);

COMMIT;
```

トランザクション実装例（予約：`reserve_send_round`）
```
-- Pseudocode (SQLite)
BEGIN IMMEDIATE;
SELECT 1 FROM sent_rounds WHERE key = :k LIMIT 1;
SELECT 1 FROM in_progress WHERE key = :k LIMIT 1;
IF none THEN
  INSERT INTO in_progress(key, round, run_id, updated_at) VALUES(:k, :round, :run_id, strftime('%s','now'));
  COMMIT;
  return True
ELSE
  ROLLBACK;
  return False
END
```

移行手順（JSON -> DB）
1. 停止：エッジプロセスを一時停止（安全な場合）。
2. バックアップ：`sent_rounds.json`, `in_progress_rounds.json`, `*.meta.json` を `backup/` にコピー。
3. 初期化：DB を作成し、PRAGMA を設定。
4. インポート：既存 JSON をパースして INSERT（`sent_rounds` / `in_progress` / `pending`）するスクリプトを実行。
5. 切替：アプリ内の読み書きを DB ラッパへ切り替え（フェーズで切替を推奨）。

代表的な分析クエリ（例）
- ラウンド別総サンプル数・参加端末数
```
SELECT run_id, round, SUM(n_samples) AS total_samples, COUNT(DISTINCT terminal_id) AS n_terminals
FROM terminal_updates
GROUP BY run_id, round
ORDER BY run_id, round;
```
- モデル配布成功率（中央→エッジ）
```
SELECT edge_id, COUNT(*) AS attempts,
 SUM(CASE WHEN http_status=200 THEN 1 ELSE 0 END) AS success_count,
 1.0*SUM(CASE WHEN http_status=200 THEN 1 ELSE 0 END)/COUNT(*) AS success_rate
FROM pushes GROUP BY edge_id;
```

API ラッパ（Python sketch）
```
import sqlite3

def _conn(path):
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.row_factory = sqlite3.Row
    return con

def reserve_send_round(con, key, round, run_id):
    cur = con.cursor()
    try:
        cur.execute('BEGIN IMMEDIATE')
        cur.execute('SELECT 1 FROM sent_rounds WHERE key=? LIMIT 1', (key,))
        if cur.fetchone():
            cur.execute('ROLLBACK')
            return False
        cur.execute('SELECT 1 FROM in_progress WHERE key=? LIMIT 1', (key,))
        if cur.fetchone():
            cur.execute('ROLLBACK')
            return False
        cur.execute('INSERT INTO in_progress(key, round, run_id, updated_at) VALUES(?,?,?,strftime("%s","now"))', (key,round,run_id))
        cur.execute('COMMIT')
        return True
    except Exception:
        cur.execute('ROLLBACK')
        raise
```

運用 & モニタリング
- 定期バックアップ（nightly）: DB ファイルをコピーして保存。DB をコピーする際は `sqlite3` の `VACUUM INTO` あるいは `online backup API` を使う。
- 成長対策: 大量ログは `events/metrics` にプルーニングルールを設ける（例: 90日で削除 / 集約）。
- CSV エクスポート機能を提供して Jupyter/Metabase 等で解析。

実験的検証プラン
1. スキーマSQL 作成 -> `edge_server/_state.sqlite3` 生成。
2. JSON のサンプルをインポート。
3. `repro_5_rounds.py --auto` を動かし、`sent_rounds` / `pending` / `in_progress` の動作確認。
4. ランリセット（新しい `run_id`）を発行して再検証。

想定される問題点（次セクションで詳細）
- 同時書き込みロック（WAL で緩和）。
- 大きなバイナリの扱い（FS に置く）。
- 旧JSON からの移行の途中での不一致。

---

付録: すぐ使える簡易インポートコマンド例（Python）
```py
import json, sqlite3, pathlib
db = sqlite3.connect('edge_server/_state.sqlite3')
cur = db.cursor()
data = json.load(open('edge_server/_aggregation_cache/some.meta.json'))
cur.execute('INSERT OR REPLACE INTO pending(filename, run_id, round, meta, saved_at) VALUES(?,?,?,?,strftime("%s","now"))',
            (data['filename'], data.get('run_id'), data.get('round'), json.dumps(data),))
db.commit()
```

---

この設計書をベースに POC 実装（コード差替え + テスト）を進めます。次は「想定エラー一覧と緩和策」を作成します。
