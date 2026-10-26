# HFL サーバ 実行ガイド

## 目次

1. [前提条件](#1-前提条件)
2. [起動方法](#2-起動方法)
3. [環境プロファイル](#3-環境プロファイル)
4. [複数エッジサーバの起動](#4-複数エッジサーバの起動)
5. [死活確認](#5-死活確認)
6. [環境変数リファレンス](#6-環境変数リファレンス)
7. [ディレクトリ構成とデータの保存先](#7-ディレクトリ構成とデータの保存先)
8. [実験データの確認](#8-実験データの確認)
9. [トラブルシューティング](#9-トラブルシューティング)

---

## 1. 前提条件

### Python バージョン

```
Python 3.9 以上
```

### 依存パッケージのインストール

```bash
pip install -r requirements.txt
```

### 実行ディレクトリ

すべてのコマンドは **プロジェクトルート**（`Serverside_HFL/`）から実行してください。

```bash
cd /path/to/Serverside_HFL
```

---

## 2. 起動方法

### 基本：対話メニューで起動

```bash
python3 start.py
```

実行すると以下のメニューが表示されます。

```
╔══════════════════════════════╗
║   HFL Server Launcher        ║
╚══════════════════════════════╝

─── 環境プロファイルを選択 ───
  1. shinomilab    研究室 (192.168.11.2)
  2. hachioji      八王子 (192.168.0.11)
  3. osaka         大阪  (192.168.68.69)
  4. localhost     同一PC (127.0.0.1)
  5. カスタム IP を入力

番号を入力 [1]:

エッジサーバの台数 [1]:
```

### オプション指定で即起動

```bash
# 環境を指定（台数は聞かれる）
python3 start.py --env shinomilab

# 環境とエッジ台数を両方指定（何も聞かれずに即起動）
python3 start.py --env shinomilab --edges 2

# 同一PCで開発・動作確認する場合
python3 start.py --env localhost --edges 1
```

### 起動後のログ表示

全サーバのログが色分けされて1画面に表示されます。

```
[中央]   INFO: Application startup complete.
[エッジ1] INFO: Application startup complete.
[エッジ2] INFO: Application startup complete.
[エッジ1] INFO: POST /receive_terminal_weights/device-001 -> 200
[中央]   INFO: POST /upload_training_metrics -> 200
```

| 色 | サーバ |
|---|---|
| 青 | 中央サーバ |
| 緑 | エッジサーバ1 |
| 黄 | エッジサーバ2 |
| マゼンタ | エッジサーバ3 |
| シアン | エッジサーバ4 |

### 停止

```
Ctrl+C
```

全サーバが一括で停止します。

---

## 3. 環境プロファイル

プロファイルは `edge_server/config.py` の `PROFILES` で管理しています。
**エッジサーバが中央サーバを見つけるための IP アドレス**を切り替えます。

| プロファイル名 | 中央サーバ URL | 用途 |
|---|---|---|
| `shinomilab` | `http://192.168.11.2:8000` | 研究室ネットワーク |
| `hachioji` | `http://192.168.0.11:8000` | 八王子ネットワーク |
| `osaka` | `http://192.168.68.69:8000` | 大阪ネットワーク |
| `localhost` | `http://127.0.0.1:8000` | 同一PCでの動作確認 |
| `custom` | 入力した IP | その他 |

### プロファイルの追加・変更

`edge_server/config.py` の `PROFILES` 辞書に追記してください。

```python
PROFILES = {
    "shinomilab": {
        "CENTRAL_SERVER_URL": "http://192.168.11.2:8000",
        "EDGE_URL":           "http://192.168.11.2:8001",
    },
    # ↓ 新しい環境を追加する場合
    "newenv": {
        "CENTRAL_SERVER_URL": "http://xxx.xxx.xxx.xxx:8000",
        "EDGE_URL":           "http://xxx.xxx.xxx.xxx:8001",
    },
}
```

追記後は `start.py` の対話メニューにも自動で反映されます（`--env newenv` でも使用可）。

---

## 4. 複数エッジサーバの起動

エッジサーバを複数台起動すると、ポートと ID が自動で割り当てられます。

```bash
python3 start.py --env shinomilab --edges 2
```

| サーバ | ポート | EDGE_SERVER_ID |
|---|---|---|
| 中央サーバ | 8000 | ─ |
| エッジサーバ1 | 8001 | `edge-server-01` |
| エッジサーバ2 | 8002 | `edge-server-02` |
| エッジサーバ3 | 8003 | `edge-server-03` |

Android 端末のアクセス先は各エッジのポートに合わせてください。

---

## 5. 死活確認

```bash
python3 start.py status
```

出力例：

```
─── サーバー死活確認 ───
  中央サーバ        ● オンライン  (HTTP 200)  http://127.0.0.1:8000/healthz
  エッジサーバ1      ● オンライン  (HTTP 200)  http://127.0.0.1:8001/healthz
  エッジサーバ2      ● オフライン             http://127.0.0.1:8002/healthz
  エッジサーバ3      ● オフライン             http://127.0.0.1:8003/healthz
  エッジサーバ4      ● オフライン             http://127.0.0.1:8004/healthz
```

ブラウザからの確認：

| URL | 内容 |
|---|---|
| `http://localhost:8000/docs` | 中央サーバ API ドキュメント |
| `http://localhost:8001/docs` | エッジサーバ1 API ドキュメント |
| `http://localhost:8000/healthz` | 中央サーバ ヘルスチェック |
| `http://localhost:8001/healthz` | エッジサーバ1 ヘルスチェック |

---

## 6. 環境変数リファレンス

`start.py` が自動で設定するものと、必要に応じて手動で上書きできるものを示します。

### start.py が自動設定する変数

| 変数名 | 内容 | 例 |
|---|---|---|
| `HFL_ENV` | 選択した環境プロファイル名 | `shinomilab` |
| `CENTRAL_SERVER_URL` | エッジが参照する中央サーバの URL | `http://192.168.11.2:8000` |
| `EDGE_URL` | エッジ自身の公開 URL | `http://192.168.11.2:8001` |
| `EDGE_SERVER_ID` | エッジサーバの識別子 | `edge-server-01` |
| `PYTHONUNBUFFERED` | ログをリアルタイム出力するため `1` に固定 | `1` |
| `CENTRAL_AGGREGATION_THRESHOLD` | （環境に未設定のときのみ）中央が待つエッジ数。既定は **エッジ台数**（全エッジ分そろってから集約） | `2`（`--edges 2` のとき） |
| `EDGE_AGGREGATION_THRESHOLD` | （環境に未設定のときのみ）各エッジが待つ端末数。`--edge-thresholds` で上書き可 | エッジ2台時の既定は **`2,1`**（端末2+1）、それ以外は各 `1` |
| `EDGE_AGGREGATION_THRESHOLD_OVERRIDE` | 上記と同じ値（エッジ集約ロジックの実効閾値用） | `EDGE_AGGREGATION_THRESHOLD` と同じ |

起動時に `集約閾値: 中央=… / 各エッジ=…` が表示されます。既にシェルで `export` 済みの変数は上書きしません。

**CLI 例**

```bash
# エッジ2台・端末閾値を明示（既定と同じ 2,1）
python3 start.py --env localhost --edges 2 --edge-thresholds 2,1

# エッジ3台・非対称な端末数
python3 start.py --env localhost --edges 3 --edge-thresholds 2,2,1
```

### 手動で上書きできる変数

起動前にシェルで `export` しておくか、`.env` ファイルを使用してください。

| 変数名 | デフォルト | 内容 |
|---|---|---|
| `CENTRAL_AGGREGATION_THRESHOLD` | `1` | 何台のエッジから更新を受け取ったらグローバル集約するか |
| `EDGE_AGGREGATION_THRESHOLD` | `1` | 何台の端末から更新を受け取ったらエッジ集約するか |
| `EDGE_POLL_INTERVAL_SECONDS` | `5` | エッジが中央サーバをポーリングする間隔（秒） |
| `MAX_UPLOAD_SIZE` | `52428800`（50MB） | 端末からの重みファイルの最大サイズ（バイト） |
| `SOURCE_DATA_PATH` | `state/training_data/latest_data.csv` | 配布する訓練データの元ファイルパス |
| `HFL_STORAGE_DIR` | プロジェクトルート | 状態・モデル・ログの保存先（OneDrive 回避用） |
| `EDGE_UPLOAD_TOKEN` | （未設定） | 設定するとエッジへの重みアップロードに Bearer 認証が必須になる |

### 設定例（実験直前に変更する場合）

```bash
# エッジ3台集まったら集約する設定で起動
export CENTRAL_AGGREGATION_THRESHOLD=3
export EDGE_AGGREGATION_THRESHOLD=5
python3 start.py --env shinomilab --edges 1
```

---

## 7. ディレクトリ構成とデータの保存先

### 通常時（OneDrive 外）

```
Serverside_HFL/
├── state/
│   ├── global_model_mobile.pt     # 現在のグローバルモデル
│   ├── current_meta.json          # 現在のラウンド情報
│   ├── app.json                   # 端末配布用設定
│   ├── training_data/
│   │   ├── latest_data.csv        # 訓練データ
│   │   └── index.jsonl            # データインデックス
│   └── training_metrics.db        # 実験メトリクス（SQLite）★
├── dist/
│   └── <timestamp>_r<round>/      # 配布済みモデル・設定のアーカイブ
├── received_edges/                # エッジから受信した集約済み重み
├── received_files/                # エッジが受信した端末の重み
├── logs/                          # タイムログ
└── archives/
    └── r<round>/                  # ラウンド別アーカイブ
```

### OneDrive 上にある場合

同期遅延を避けるため、データは自動的に `~/hfl_data/` に保存されます。
`HFL_STORAGE_DIR` で任意のパスに変更できます。

```bash
export HFL_STORAGE_DIR=/tmp/hfl_data
python3 start.py --env localhost
```

---

## 8. 実験データの確認

### メトリクス DB の参照

実験メトリクスは `state/training_metrics.db` に蓄積されます。

```bash
sqlite3 state/training_metrics.db
```

#### アプリ種別ごとの満足度改善を集計

```sql
SELECT
    app_type,
    COUNT(*)                                      AS n_experiments,
    ROUND(AVG(satisfaction_before), 3)            AS avg_before,
    ROUND(AVG(satisfaction_after), 3)             AS avg_after,
    ROUND(AVG(satisfaction_after - satisfaction_before), 3) AS avg_improvement
FROM metrics
WHERE satisfaction_before IS NOT NULL
  AND satisfaction_after  IS NOT NULL
GROUP BY app_type
ORDER BY avg_improvement DESC;
```

#### ラウンド別の精度推移

```sql
SELECT
    round,
    COUNT(*)              AS n_edges,
    ROUND(AVG(accuracy), 4) AS avg_accuracy,
    ROUND(AVG(loss), 4)     AS avg_loss
FROM metrics
WHERE accuracy IS NOT NULL
GROUP BY round
ORDER BY round;
```

#### 全データをCSVに書き出す

```bash
sqlite3 -header -csv state/training_metrics.db \
    "SELECT * FROM metrics ORDER BY id;" \
    > metrics_export.csv
```

### ログの集約（既存ツール）

```bash
# 最新日付のログを自動検出して集約
python tools/shared_storage_rollup.py --latest --outdir logs/analysis --format both
```

---

## 9. トラブルシューティング

### サーバが起動しない

**`ModuleNotFoundError`** が出る場合：

```bash
pip install -r requirements.txt
```

**ポートが使用中** のエラーが出る場合：

```bash
# 使用中のプロセスを確認（macOS/Linux）
lsof -i :8000
lsof -i :8001

# プロセスを終了
kill -9 <PID>
```

---

### エッジサーバが中央サーバに接続できない

1. `python3 start.py status` で中央サーバが `オンライン` か確認する
2. 選択した環境プロファイルの IP が正しいか確認する
3. ファイアウォールがポート 8000 を許可しているか確認する

---

### `healthz` は通るが Android 端末から繋がらない

端末と PC が同じネットワークに接続されているか確認してください。
エッジサーバは `0.0.0.0` でリッスンしているため、PC の **LAN IP** でアクセスします。

```bash
# macOS で LAN IP を確認する
ifconfig | grep "inet " | grep -v 127.0.0.1
```

---

### メトリクス DB にデータが入らない

`training_metrics.db` が存在しない場合は中央サーバ起動時に自動作成されます。
データが入らない場合は以下を確認してください。

1. Android 端末が `accuracy` / `app_type` / `satisfaction_before` / `satisfaction_after` を送信しているか
2. エッジサーバのログに `training_metrics_sent` が出ているか
3. 中央サーバのログに `training_metrics_received` が出ているか

---

### Windows で実行する場合

`python3` が使えない環境では `python` に読み替えてください。

```powershell
python start.py --env shinomilab --edges 1
```
