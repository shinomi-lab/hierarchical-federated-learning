# Android クライアント 提案＋仕様（v1）

本書は、端末（Android）側の実装提案と、現行エッジサーバの実エンドポイントに準拠した仕様まとめです。短時間で実装可能な最小構成と、運用で効く推奨事項（ログのflush順序、接続ジッター、リトライ方針等）を併記します。

## 1. 前提・認証
- HTTPは `Authorization: Bearer <token>`（環境 `EDGE_UPLOAD_TOKEN` を設定時に有効）。
- WebSocketは `GET /ws/updates?token=<WS_AUTH_TOKEN>`（環境 `WS_AUTH_TOKEN` を設定時に照合）。
- TLSはネットワーク環境に応じて推奨。

## 2. エンドポイント一覧（現行）
- メタ取得: `GET /api/v1/meta`
  - 429時は `Retry-After` を尊重（例: 10秒）。
- 学習結果アップロード: `POST /receive_terminal_weights/{terminal_id}`（multipart/form-data）
  - ラウンド厳密一致。差異があると 409 + `{"error":"round_mismatch","current_round":..,"next_meta_url":"/api/v1/meta"}` + `Retry-After: 5`。
- ログアップロード: `POST /upload_client_logs/{terminal_id}`（multipart/form-data, 任意パート名可）
  - `meta`（JSON文字列）をFormで付与すると `{"round":31}` 等でパーティションに利用。
  - 1ファイル上限超過で 413 + `Retry-After: 60`。
- 端末テレメトリ: `POST /api/telemetry/ingest/{terminal_id}`（application/json）
  - 413 + `Retry-After: 60`（ペイロード過大時）。
- WebSocket通知: `GET /ws/updates?token=...`
  - 受信メッセージ例: `{ "type": "model_update", "payload": { "schema_version":1, "notification_id":"...", "round":31, "download_rel":"...", "download_url":null, "description":"...", "timestamp":"YYYYMMDD_HHMMSS", "delay_hint_ms":500 } }`

補足: 端末側ACK用の専用HTTPエンドポイントは現行コードにはありません（将来追加予定）。ACKはログ／テレメトリで代替可能です。

## 3. 推奨クライアント実装（提案）
- ラウンド同期:
  - 学習→5エポック毎に1回送信→409なら `Retry-After` 待機→`/api/v1/meta` 再取得→端末ラウンド更新。
- ログ完全性:
  - 「学習完了→log flush/close→数秒待機→アップロード」。ローテーション済み（クローズ済み）ファイルのみ送信。
  - 同一内容の再送は許容（サーバはSHA-256で重複排除）。
- ダウンロード突入平準化:
  - WSの `delay_hint_ms` を±20%でジッター。開始時刻を分散。
- 送信スケジューリング:
  - 小さなJSON（テレメトリ）は即時／定期（例: 60秒毎）。
  - 大きなログは充電中・Wi-Fi時を優先。WorkManagerで `requiresCharging=true`/`unmetered` を条件化。
- リトライ方針:
  - 413/429は `Retry-After` 厳守。その他は指数バックオフ（初回2s→4s→8s、最大60s、3〜5回）。
- セキュリティ/時刻:
  - トークン期限切れ時は再認証。端末時計の大幅ずれに注意（`timestamp_ms` はNTP等で矯正）。

## 4. 具体仕様
### 4.1 メタ
- `GET /api/v1/meta` → `{ "edge_id": "edge-server-01", "round": 31, "upload_endpoint": "/receive_terminal_weights/{terminal_id}", ... }`
- 429時: `{ "error":"rate_limit_exceeded", "wait_seconds": <float> }` + `Retry-After` ヘッダ。

### 4.2 学習結果アップロード（weights）
- `POST /receive_terminal_weights/{terminal_id}`（multipart）
  - 必須Formの例（実装に合わせて送信）: `round_id`, `model_id`, `base_hash`, `n_samples`, `dtype`, `payload_kind`, `weights`(ファイル)
  - 成功: `{ "status":"weights_received", "ack":true, ... }`
  - ラウンド不一致: 409 + 上記JSON + `Retry-After: 5`

### 4.3 ログアップロード（multipart）
- `POST /upload_client_logs/{terminal_id}`
  - 任意パート名で複数ファイル可。
  - 任意Form `meta`（JSON文字列: 例 `{ "round": 31 }`）。
  - 成功: `{ "ack":true, "status":"stored|duplicate_ignored", "received":[...] }`
  - 413: `{ "ack":false, "status":"too_large", ... }` + `Retry-After: 60`
  - 保存: `received_files/terminal_logs/{terminal_id}/r{round}/YYYYMMDD/` + `index.jsonl`

### 4.4 端末テレメトリ（JSON）
- `POST /api/telemetry/ingest/{terminal_id}`
  - 例:
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
  - 成功: `{ "ack": true, "status":"ok" }`
  - 保存: `received_files/terminal_telemetry/{terminal_id}/index.jsonl` に追記（日次ディレクトリ分割、保持期間 `LOG_RETENTION_DAYS`）。

### 4.5 WebSocket
- `GET /ws/updates?token=...`
- 受信時フロー: `delay_hint_ms` 待機（±20%ジッター）→ `download_url`（あれば）/ `download_rel` を解決→モデル取得→適用→（任意で）ログ/テレメトリへ記録。

## 5. Kotlin 実装概略
### 5.1 WorkManager（大きなログのアップロード）
```kotlin
val constraints = Constraints.Builder()
  .setRequiredNetworkType(NetworkType.UNMETERED) // Wi-Fi
  .setRequiresCharging(true)
  .build()

val work = OneTimeWorkRequestBuilder<UploadLogsWorker>()
  .setConstraints(constraints)
  .build()
WorkManager.getInstance(context).enqueue(work)
```

### 5.2 ログ送信（OkHttp）
```kotlin
val body = MultipartBody.Builder().setType(MultipartBody.FORM)
  .addFormDataPart("meta", "{\"round\":$round}")
  .addFormDataPart("edge_server.log", "edge_server.log",
    logFile.asRequestBody("text/plain".toMediaType()))
  .build()
val req = Request.Builder()
  .url("$edgeBase/upload_client_logs/$terminalId")
  .addHeader("Authorization", "Bearer $token")
  .post(body)
  .build()
```

### 5.3 テレメトリ送信（OkHttp）
```kotlin
val json = "{" +
  "\"round\":$round," +
  "\"timestamp_ms\":${System.currentTimeMillis()}," +
  "\"battery_level\":$battery," +
  "\"is_charging\":$isCharging" +
  "}"
val req = Request.Builder()
  .url("$edgeBase/api/telemetry/ingest/$terminalId")
  .addHeader("Authorization", "Bearer $token")
  .post(json.toRequestBody("application/json".toMediaType()))
  .build()
```

### 5.4 WebSocket（受信→遅延→取得）
```kotlin
val request = Request.Builder()
  .url("$wsBase/ws/updates?token=$wsToken")
  .build()
val client = OkHttpClient()
val ws = client.newWebSocket(request, object: WebSocketListener() {
  override fun onMessage(webSocket: WebSocket, text: String) {
    val root = JSONObject(text)
    if (root.optString("type") == "model_update") {
      val p = root.getJSONObject("payload")
      val delayMs = p.optLong("delay_hint_ms", 0L)
      val jitter = (delayMs * (0.8 + Math.random()*0.4)).toLong()
      Handler(Looper.getMainLooper()).postDelayed({
        val rel = p.optString("download_rel", null)
        val abs = p.optString("download_url", null)
        val url = abs ?: (edgeBase.trimEnd('/') + "/download_model/" + rel)
        // TODO: download and apply
      }, jitter)
    }
  }
})
```

## 6. テスト観点・運用
- 負荷テスト: 2〜3台同時接続・並列送信で 429/413/409 の扱いを確認。
- ロータリング: ログflush直後のアップロードで内容の欠落がないかを確認。
- 回線: モバイル回線時はサイズ上限内のテレメトリのみ送信、ログはWi-Fi待ち。

## 7. 既知の注意点
- `CLIENT_SPEC_SUMMARY.md` で暫定URL例を出している箇所がありますが、現行コードの実エンドポイントは本書に記載の通りです（例: ログは `/upload_client_logs/{terminal_id}`）。
- 端末側ACK専用APIは未提供のため、必要に応じて後日 `/api/device/ack` を追加予定です。

---
不明点や実装補助（完全な Retrofit インターフェース／WSラッパ）も対応可能です。必要なら `docs/android/kotlin_samples/` を作成して追加します。
