# 端末向け — 学習データ送信 API 仕様 (central)

この文書は端末（クライアント）開発者に渡してください。サーバ側は以下の API を受け取り、検証・保存します。

エンドポイント（メトリクス）
- URL: POST `/api/training-data`
- コンテンツタイプ: `application/json`

目的
- 端末がローカルトレーニングのメトリクス・リソース使用量・データ分布などを中央サーバへ送信し、後段で可視化や分析を行うため。

リクエスト JSON スキーマ（例）
```json
{
  "roundNumber": 1,
  "epoch": 10,
  "batchSize": 32,
  "learningRate": 0.001,
  "trainingLoss": 0.123,
  "validationLoss": 0.456,
  "trainingAccuracy": 0.89,
  "validationAccuracy": 0.78,
  "uploadSize": 2048,
  "downloadSize": 1024,
  "communicationTime": 1500,
  "cpuUsage": 45.5,
  "memoryUsage": 256.0,
  "batteryUsage": 5.0,
  "datasetSize": 10000,
  "timestamp": 1730793600000,
  "communicationErrors": 0,
  "trainingErrors": 1,
  "dataDistribution": {"classA": 5000, "classB": 5000},
  "preprocessingTime": 3000
}
```

必須フィールド（バリデーション）
- roundNumber (int)
- epoch (int)
- batchSize (int)
- learningRate (float)
- trainingLoss (float)
- validationLoss (float)
- trainingAccuracy (float)
- validationAccuracy (float)
- uploadSize (int)
- downloadSize (int)
- communicationTime (int)
- cpuUsage (float)
- memoryUsage (float)
- batteryUsage (float)
- datasetSize (int)
- timestamp (int)  // epoch ミリ秒

オプション
- communicationErrors (int, default 0)
- trainingErrors (int, default 0)
- dataDistribution (object) — JSON マップ
- preprocessingTime (int, ms)

レスポンス
- 成功（保存完了）: HTTP 200
```json
{ "status": "ok", "id": 123 }
```
- バリデーションエラー: HTTP 400
```json
{ "detail": "validation_error", "errors": [ {"loc": [...], "msg": "...", "type": "..."}, ... ] }
```
- サーバ内部エラー: HTTP 500
```json
{ "detail": "internal_error", "error": "..." }
```

保存先
- 現在は組み込み SQLite を使用: `central_server/training_data.db` に `training_data` テーブルとして永続化されます。

注意点（端末実装者へ）
- リクエストは JSON 構造を厳密に守ってください。型エラーがあると 400 を返します。
- タイムスタンプはミリ秒 epoch を推奨します（サーバ側でそのまま保存します）。

簡易実装サンプル（Python requests）
```python
import requests
url = "http://<central-host>:8000/api/training-data"
payload = { ... }  # 上の JSON
resp = requests.post(url, json=payload, timeout=30)
print(resp.status_code, resp.text)
```

テスト手順（運用上推奨）
1. 開発環境の central を起動する（例: `uvicorn central_server.main:app --port 8000`）。
2. 上記サンプルで POST を投げ、HTTP 200 と `{"status":"ok"}` が返ることを確認。
3. DB の内容を確認（`central_server/training_data.db` を sqlite3 で開く）。
4. 不正な型のフィールドを送って 400 が返ることを確認。

ログ
- 受信成功は `central_time_logger` に `training_data_received` イベントとして記録されます。
- バリデーションエラーは 400 として応答し、`central_time_logger` に `validation_error` イベントを記録します。
- サーバ内例外は通常の 500 応答とログへ記録されます。

拡張 / 可視化
- DB から集計して Streamlit / Grafana などでダッシュボード化できます。必要なら視覚化テンプレートを作成します。

連絡
- 実装・デプロイ後、端末チームは E2E テストを実施して結果（成功ログ、端末送信ログ）を共有してください。私の方で central ログと照合して確認します。

---

## 原データ（サンプル）アップロード API（multipart）

目的
- 画像・テキストなどの学習サンプルをそのまま中央へ収集し、メタ情報とともにインデックス化します。

エンドポイント（サンプル）
- URL: POST `/api/training-data/upload-sample`
- コンテンツタイプ: `multipart/form-data`
- フィールド
  - `file`: バイナリ（必須）
  - `device_id`: 文字列（必須）
  - `round_number`: 整数（必須）
  - `label`: 文字列（任意）
  - `sample_type`: 文字列（任意, 例: `image`, `text`）
  - `timestamp`: 整数（任意, ms epoch）

保存レイアウト
- `TRAINING_DATA_DIR/<device_id>/r<round>/<sha256>/<original_filename>`
- `manifest.json`（同ディレクトリ）にメタを書き出し
- `TRAINING_DATA_DIR/index.jsonl` に1行1レコードでインデックス追記

レスポンス例
```json
{ "status": "ok", "sha256": "<sha256>" }
```

リスト取得
- GET `/api/training-data/index?device_id=...&round_number=...&label=...&limit=100`
- 応答: `{ "items": [...], "count": N }`

ダウンロード
- GET `/api/training-data/download/{sha}`
- 最初の一致レコードを返送します（`index.jsonl` を探索）。

簡易クライアント例（Python requests）
```python
import requests
url = "http://<central-host>:8000/api/training-data/upload-sample"
files = {"file": open("sample.jpg", "rb")}
data = {
    "device_id": "device-001",
    "round_number": 1,
    "label": "cat",
    "sample_type": "image",
}
resp = requests.post(url, files=files, data=data, timeout=60)
print(resp.status_code, resp.text)
```

注意
- 同一バイナリは `sha256` に基づいてディレクトリが同じになります（重複排除の指標）。
- 端末側で送信リトライを行う場合、同じ `sha256` でも安全に上書きされます。
