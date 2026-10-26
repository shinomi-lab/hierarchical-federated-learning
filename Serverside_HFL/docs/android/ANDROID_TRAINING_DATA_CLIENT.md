# Android/Kotlin — Training Data Client Guide

このガイドは Android (Kotlin) クライアントが中央サーバへ学習メトリクスと原データサンプルを送信するための最小実装例です。Retrofit/OkHttpを用いた構成を想定しています。

- 中央サーバURL例: `http://192.168.11.2:8000`
- メトリクス送信: `POST /api/training-data` (JSON)
- サンプル送信: `POST /api/training-data/upload-sample` (multipart)

## データモデル (JSON)
```json
{
  "roundNumber": 5,
  "terminalId": "device-001",
  "epoch": 10,
  "batchSize": 32,
  "learningRate": 0.001,
  "trainingLoss": 0.12,
  "validationLoss": 0.34,
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
  "trainingErrors": 0,
  "dataDistribution": {"A": 5000, "B": 5000},
  "preprocessingTime": 3000
}
```

## Retrofit インターフェイス
```kotlin
import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.Call
import retrofit2.http.*

interface TrainingDataApi {
    @POST("/api/training-data")
    fun sendMetrics(@Body body: TrainingMetrics): Call<MetricsResponse>

    @Multipart
    @POST("/api/training-data/upload-sample")
    fun uploadSample(
        @Part file: MultipartBody.Part,
        @Part("device_id") deviceId: RequestBody,
        @Part("round_number") roundNumber: RequestBody,
        @Part("label") label: RequestBody?,
        @Part("sample_type") sampleType: RequestBody?,
        @Part("timestamp") timestamp: RequestBody?
    ): Call<UploadResponse>
}
```

## データクラス (Kotlin)
```kotlin
data class TrainingMetrics(
    val roundNumber: Int,
    val terminalId: String?,
    val epoch: Int,
    val batchSize: Int,
    val learningRate: Double,
    val trainingLoss: Double,
    val validationLoss: Double,
    val trainingAccuracy: Double,
    val validationAccuracy: Double,
    val uploadSize: Int,
    val downloadSize: Int,
    val communicationTime: Int,
    val cpuUsage: Double,
    val memoryUsage: Double,
    val batteryUsage: Double,
    val datasetSize: Int,
    val timestamp: Long,
    val communicationErrors: Int = 0,
    val trainingErrors: Int = 0,
    val dataDistribution: Map<String, Int>?,
    val preprocessingTime: Int = 0,
)

data class MetricsResponse(val status: String, val id: Long?, val ackTimestamp: Long?)

data class UploadResponse(val status: String, val sha256: String?)
```

## OkHttp/Retrofit 初期化
```kotlin
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory

fun createApi(baseUrl: String): TrainingDataApi {
    val client = OkHttpClient.Builder()
        .retryOnConnectionFailure(true)
        .build()
    return Retrofit.Builder()
        .baseUrl(baseUrl)
        .client(client)
        .addConverterFactory(MoshiConverterFactory.create())
        .build()
        .create(TrainingDataApi::class.java)
}
```

## メトリクス送信例
```kotlin
val api = createApi("http://192.168.11.2:8000")
val metrics = TrainingMetrics(
    roundNumber = 5,
    terminalId = "device-001",
    epoch = 10,
    batchSize = 32,
    learningRate = 0.001,
    trainingLoss = 0.12,
    validationLoss = 0.34,
    trainingAccuracy = 0.89,
    validationAccuracy = 0.78,
    uploadSize = 2048,
    downloadSize = 1024,
    communicationTime = 1500,
    cpuUsage = 45.5,
    memoryUsage = 256.0,
    batteryUsage = 5.0,
    datasetSize = 10000,
    timestamp = System.currentTimeMillis(),
    communicationErrors = 0,
    trainingErrors = 0,
    dataDistribution = mapOf("A" to 5000, "B" to 5000),
    preprocessingTime = 3000,
)
val resp = api.sendMetrics(metrics).execute()
println("metrics status=${resp.code()} body=${resp.body()} errors=${resp.errorBody()?.string()}")
```

## サンプルアップロード例 (multipart)
```kotlin
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.RequestBody

fun uploadSampleFile(api: TrainingDataApi, fileBytes: ByteArray) {
    val fileBody = RequestBody.create("application/octet-stream".toMediaTypeOrNull(), fileBytes)
    val part = MultipartBody.Part.createFormData("file", "sample.bin", fileBody)
    val deviceId = RequestBody.create("text/plain".toMediaTypeOrNull(), "device-001")
    val roundNumber = RequestBody.create("text/plain".toMediaTypeOrNull(), "5")
    val label = RequestBody.create("text/plain".toMediaTypeOrNull(), "cat")
    val sampleType = RequestBody.create("text/plain".toMediaTypeOrNull(), "image")
    val timestamp = RequestBody.create("text/plain".toMediaTypeOrNull(), System.currentTimeMillis().toString())
    val resp = api.uploadSample(part, deviceId, roundNumber, label, sampleType, timestamp).execute()
    println("upload status=${resp.code()} body=${resp.body()} errors=${resp.errorBody()?.string()}")
}
```

## 実装ノート
- 送信タイミング: 5グローバルラウンド終了毎にまとめて送信。
- リトライ: `retryOnConnectionFailure(true)` を有効化、メトリクスは端末ID×ラウンドで重複無視されます。
- セキュリティ: 必要なら `Authorization: Bearer <token>` をヘッダに追加（サーバ側は拡張可能）。
- ポーリング/WS: ラウンド検知はWS通知が推奨。フォールバックは5–10秒間隔のHTTPポーリング。WS仕様は `WEBSOCKET_SPEC.md` を参照。

## テストチェックリスト
- 成功時: 200、`{"status":"ok"}`（メトリクス）/ `{"status":"ok","sha256":"..."}`（サンプル）
- DB/保存: `training_data.db` に行追加、`state/training_data` 配下に保存、`index.jsonl` に追記。
- アーカイブ: ラウンド完了後、`archives/r<round>/` に一式がまとまること。

---

## クライアントログアップロード API（Android 向け確認用仕様）

端末の診断/研究用ログをエッジサーバに送るためのAPI仕様です。以下に従っていれば「合っている」と判断できます。

### 前提と認証
- ベースURL: `EDGE_BASE_URL`（例: `http://192.168.11.2:8001`）
- 認証: `Authorization: Bearer <EDGE_UPLOAD_TOKEN>` ヘッダー必須
- 端末ID: `terminal_id` をパスパラメータに指定

### エンドポイント
- `POST /upload_client_logs/{terminal_id}`
  - Content-Type: `multipart/form-data`
  - フィールド:
        - `meta`（任意・文字列）: JSON（例: `{ "terminal_id": "device-001", "round": 12, "timestamp_ms": 1710000000000 }`）
        - ファイルパートは任意のフィールド名（`file_0` 以外でも可）に対応（サーバ側で自動検出）
  - 返り値（200）: `{"ack":true,"status":"stored|duplicate_ignored","received":[{"name":"...","size":1234,"sha256":"sha256:..."}]}`
  - エラー: 400/401/413/429/5xx

- `GET /upload_client_logs/{terminal_id}/{sha256}`
  - 目的: 指定SHAの既存確認
  - 返り値: `{"exists":true|false}`

### サイズ制限（既定）
- 全体: `MAX_UPLOAD_SIZE`（既定50MB）
- 1ファイル: `MAX_LOG_UPLOAD_SIZE_PER_FILE`（既定10MB）

### 保存先と重複
- 保存: `received_files/terminal_logs/{terminal_id}/{sha_hex}.log`
- 重複: 同一SHAは `duplicate_ignored` としてACKのみ返す

### Kotlin サンプル（Retrofit）
```kotlin
interface ClientLogsApi {
    @Multipart
    @POST("upload_client_logs/{terminalId}")
    suspend fun uploadClientLogs(
        @Path("terminalId") terminalId: String,
        @Part("meta") meta: RequestBody?,
        @Part files: List<MultipartBody.Part>,
        @Header("Authorization") auth: String
    ): Response<UploadResult>

    @GET("upload_client_logs/{terminalId}/{sha256}")
    suspend fun checkExists(
        @Path("terminalId") terminalId: String,
        @Path("sha256") sha256: String
    ): Response<ExistsResult>
}

data class UploadResult(
    val ack: Boolean,
    val status: String,
    val received: List<ReceivedFile>
)
data class ReceivedFile(val name: String, val size: Long, val sha256: String)
data class ExistsResult(val exists: Boolean)

suspend fun uploadLogs(
    api: ClientLogsApi,
    terminalId: String,
    token: String,
    files: List<File>,
    metaJson: String?
): UploadResult? {
    val auth = "Bearer $token"
    val parts = files.mapIndexed { idx, file ->
        val rb = file.asRequestBody("application/octet-stream".toMediaType())
        MultipartBody.Part.createFormData("file_$idx", file.name, rb)
    }
    val metaBody = metaJson?.toRequestBody("application/json".toMediaType())
    val resp = api.uploadClientLogs(terminalId, metaBody, parts, auth)
    if (resp.isSuccessful) return resp.body()
    throw HttpException(resp)
}
```

### 実装チェック（Android側）
- `Authorization` ヘッダー必須
- 413時は再送停止・分割
- `received[].sha256` を保持し重複制御
- 必要なら事前に `GET` で存在確認

---

## 端末側の変更ポイント（チェックリスト）

このプロジェクトの更新に伴い、Android クライアント側で最低限以下の修正・確認が必要です。

- 認証ヘッダー: すべてのアップロード（重み・ログ）で `Authorization: Bearer <EDGE_UPLOAD_TOKEN>` を付与。トークン失効時の再取得/再送のハンドリングを追加。
- ラウンド整合: 重みアップロードで 409 受信時はレスポンス本文の `current_round` を読み取り、`/api/v1/meta` ポーリング（またはWS通知）で同期後に再試行。
    - サーバ側は `Retry-After: 5` ヘッダと `next_meta_url` を付与（クライアント変更不要）。
    - WS通知のペイロードは統一スキーマ（`payload.schema_version=1`）で配信。`notification_id` をACK/イベントで返送。
- 学習アップロードのタイミング: 「5エポック（1サイクル）完了後に1回のみ」送信するロジックへ統一。送信後はラウンド更新を待機（`/api/v1/meta`）。
- ログアップロードの `meta`: `{"terminal_id":"...","round":<int>,"timestamp_ms":<long>}` を推奨。これによりサーバ側保存が `terminal_logs/{terminal_id}/r{round}/YYYYMMDD/` に自動分割される。
- サイズ制御: 合計50MB・1ファイル10MBを超えないよう送信側で制御。413時は再送停止してサイズ削減（分割/圧縮/選別）。
- 重複抑止: 成功応答の `received[].sha256` を保持し、必要に応じて送信前に `GET /upload_client_logs/{terminal_id}/{sha256}` で存在確認。
- リトライ戦略: ネットワーク例外や一時的な 5xx に対し、指数バックオフ＋ジッターのリトライ（1〜3回）を実装。
- ファイル命名規則: `YYYYMMDD_HHMMSS_<tag>.log` 等の統一フォーマットで送信すると検索が容易。

これらが満たされていれば、端末台数が増えてもラウンドごと・端末ごとにログ/データが整理され、運用と分析の双方がスムーズになります。