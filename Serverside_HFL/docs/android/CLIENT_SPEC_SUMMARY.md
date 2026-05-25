# Android クライアント仕様サマリー（v1.1）

端末（Android）実装に必要な要点を簡潔に整理したサマリーです。詳細は `docs/android/WEBSOCKET_SPEC.md` と `docs/android/ANDROID_TRAINING_DATA_CLIENT.md` を参照してください。

## 認証
- トークン方式（Bearer）を使用。HTTP/WS いずれも `Authorization: Bearer <token>` を付与。
- トークン失効時は 401/403 を受け取り、再認証フローへ。

## ラウンド進行と送信ルール
- 端末は「5エポック学習後に1回送信」し、その後はラウンド更新を待機。
- ラウンド不一致（端末が `r` を送るが、エッジの現在ラウンドが異なる）時は 409 が返る。
  - レスポンス JSON（例）：
    ```json
    {"error":"round_mismatch","current_round":31,"next_meta_url":"/api/meta","retry_after":5}
    ```
  - ヘッダ：`Retry-After: 5`（秒）。指定秒数後にメタ API（例：`GET /api/meta`）へ再問い合わせ。
  - 推奨フロー：409 → `Retry-After` 待機 → メタ取得 → 端末ラウンド更新 → 次の送信タイミングまで待機。

## メタ情報取得（例）
- エンドポイント例：`GET /api/meta`
- レート制限に達した場合は 429 と `Retry-After` が返るため、指定秒数後に再試行。
- 例：`GET /api/meta` に対して 429 (`Retry-After: 10`) → 10 秒後に再試行。

## 学習結果アップロード（ログ含む）
- モデル・メタ送信は既存ルートに従う（詳細は Android クライアントガイド参照）。
- ログアップロード API（multipart/form-data）：任意のパート名で複数ファイル可。
  - 保存先は `terminal_logs/{terminal_id}/r{round}/YYYYMMDD/` に自動振り分け。
  - SHA-256 ハッシュで重複排除。`index.jsonl` に追記される。
  - 1ファイルの上限：`MAX_LOG_UPLOAD_SIZE_PER_FILE`（サーバ設定）。超過時は 413 と `Retry-After: 60`（秒）を返す。
  - 古いログは `LOG_RETENTION_DAYS` ポリシーで自動削除対象（ベストエフォート）。

  #### 成功レスポンス（例）
  ```json
  {"status":"ok","stored_files":[{"name":"train.log","sha256":"..."}]} 
  ```

  #### エラーレスポンス（例）
  ```json
  {"error":"file_too_large","retry_after":60}
  ```

## 端末テレメトリ送信（バッテリー・端末状態 等）
- エンドポイント：`POST /api/telemetry/ingest/{terminal_id}`（JSON）
- ヘッダ：`Authorization: Bearer <token>`（必要な場合）
- ボディ例：
```json
{
  "round": 31,
  "timestamp_ms": 1733817600000,
  "battery_level": 0.72,
  "is_charging": true,
  "temperature_c": 36.5,
  "cpu_usage": 0.35,
  "mem_used_mb": 1024,
  "storage_free_mb": 20480,
  "network_rssi": -65,
  "network_type": "WIFI",
  "app_version": "1.3.0",
  "device_model": "Pixel 7",
  "os_version": "Android 14",
  "extras": {"screen_on": true}
}
```
- サーバ保存：`received_files/terminal_telemetry/{terminal_id}/index.jsonl` に追記（1行1イベント）。日付別ディレクトリにパーティション。`LOG_RETENTION_DAYS` に基づく古い日の削除あり（ベストエフォート）。
- 413 時は `Retry-After: 60`。JSONは小さく保つ（既定上限 `MAX_TELEMETRY_PAYLOAD_BYTES`）。

### OkHttp/Retrofit 送信例（概略）
```kotlin
val requestBody = MultipartBody.Builder().setType(MultipartBody.FORM)
    .addFormDataPart("file1", "train.log", file1.asRequestBody("text/plain".toMediaType()))
    .addFormDataPart("file2", "metrics.json", file2.asRequestBody("application/json".toMediaType()))
    .build()

val req = Request.Builder()
    .url(edgeBaseUrl + "/api/logs/upload")
    .addHeader("Authorization", "Bearer $token")
    .post(requestBody)
    .build()
```

### ACK 送信例（Kotlin 概略）
```kotlin
data class AckBody(
  val round: Int,
  val latency_ms: Long,
  val status: String = "applied"
)

val ackReq = Request.Builder()
  .url(edgeBaseUrl + "/api/device/ack")
  .addHeader("Authorization", "Bearer $token")
  .post(
    "{" +
      "\"round\":$round," +
      "\"latency_ms\":$latency," +
      "\"status\":\"applied\"" +
    "}".toRequestBody("application/json".toMediaType())
  )
  .build()
```

## WebSocket 通知（統一ペイロード）
- サーバ仕様は `docs/android/WEBSOCKET_SPEC.md` に詳細あり。
- 受信ペイロード（例）：
```json
{
  "schema_version": 1,
  "notification_id": "c0a5c...",
  "round": 31,
  "download_rel": "/api/model/download",
  "download_url": "https://edge.example.com/api/model/download",
  "description": "New aggregated model is ready",
  "timestamp": "2025-07-11T12:34:56Z",
  "delay_hint_ms": 500
}
```
- `delay_hint_ms`: 端末はこの遅延ヒントに従い、ダウンロード開始前にランダム化（±20%程度）して待機することで同時突入を緩和。

### WS クライアント処理（概略）
- 接続時に `Authorization: Bearer <token>` を付与。
- 受信ごとに `notification_id` を重複排除用に記録（同一IDは一度だけ処理）。
- `delay_hint_ms` を用いた待機後、`download_url` または `download_rel` を解決して取得。

## ACK とイベント送信
- モデルダウンロード完了・適用後に ACK を送信（HTTP）。
- ACK 時に `latency_ms` 等を含めると、サーバ側メトリクスに反映される（P50/P90）。

## エラー処理とリトライベストプラクティス
- 413/429 は `Retry-After` を尊重。指定秒数後に再試行。
- ネットワーク失敗は指数バックオフ（最大 3〜5 回、上限 60 秒）。
- 409（round mismatch）はメタ問い合わせ経由で同期を取り直す。
- WS は再接続時にジッター付きバックオフ。

## リトライ方針（共通）
- 413/429 は `Retry-After` を尊重。指定秒数後に再試行。
- ネットワーク失敗は指数バックオフ（最大 3〜5 回）。
- 409（round mismatch）はメタ問い合わせ経由で同期を取り直す。

## 推奨クライアント設定
- タイムアウト：HTTP 接続/読み取り 30〜60 秒。
- 同時転送数を控えめに（2〜3）。
- WS 再接続は指数バックオフ＋ジッター。

## 実装チェックリスト
- 5エポック後送信＋ラウンド待機の遵守
- 409 受信時：`Retry-After` 待機→メタ再取得→ラウンド更新
- ログ送信：multipart、サイズ上限、重複排除、失敗時 `Retry-After` 準拠
- WS：`schema_version=1`、`notification_id` 重複排除、`delay_hint_ms` 適用
- モデル取得後：ACK 送信（`round`、`latency_ms`）
- 共通リトライ：429/413/ネットワーク失敗でバックオフ＋上限

## 端末側の必須対応一覧
- 5エポック後1回送信＋ラウンド待機の遵守。
- 409（`current_round`）を受けたら `Retry-After` 後にメタ再取得。
- WS 統一ペイロードの処理（`schema_version=1`）。
- `delay_hint_ms` を使ったダウンロード開始の平準化。
- ログアップロード時のサイズ上限・重複排除を意識し、失敗時は `Retry-After` を尊重。
- ACK（ダウンロード・適用）を送信してメトリクスに寄与。

## 参照資料
- WebSocket 仕様: `docs/android/WEBSOCKET_SPEC.md`
- Android クライアントガイド: `docs/android/ANDROID_TRAINING_DATA_CLIENT.md`

---
このサマリーで不足があれば、端末側のコード雛形（Kotlin）も追補可能です。
