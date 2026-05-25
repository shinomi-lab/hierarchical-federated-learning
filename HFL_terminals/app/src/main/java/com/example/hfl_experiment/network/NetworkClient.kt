// com/example/hfl_experiment/network/NetworkClient.kt
package com.example.hfl_experiment.network

import android.content.Context
import android.content.SharedPreferences
import com.example.hfl_experiment.AppConfig
import com.example.hfl_experiment.network.api.ApiService
import com.example.hfl_experiment.network.api.MetaResp
import com.example.hfl_experiment.network.api.SendToDeviceDto
import com.example.hfl_experiment.storage.SendHistoryDbHelper
import com.example.hfl_experiment.util.PerfLogger
import com.example.hfl_experiment.util.logging.RealTimeLogger
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.ResponseBody.Companion.toResponseBody
import org.json.JSONObject
import retrofit2.HttpException
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory

/** POST /upload_client_logs の非 2xx。HTTP コードを保持し 4xx の無限リトライを避ける。 */
class UploadClientLogsHttpException(
    val httpCode: Int,
    val responseBody: String?,
) : IOException("uploadClientLogs failed: http=$httpCode body=${responseBody?.take(500)}")

/**
 * Edge サーバとの通信クライアント（堅牢版）
 *
 * 機能:
 * - メタ取得: GET /api/v1/meta
 * - 端末向け配布情報: GET /api/v1/send_to_device
 * - 学習結果アップロード（flat float32 or torch_state_dict）: POST /receive_terminal_weights/{terminal_id}
 *
 * 改善点:
 * - BaseURL の正規化
 * - タイムアウト明示 & OkHttpClient を外部注入可能
 * - 軽量なリトライ (指数バックオフ/キャンセル対応)
 * - 例外ログの充実（HTTP, IO, それ以外）
 * - f32_flat 送信時のメタパラメータ(input/hidden/output)の必須化とワイヤリング修正
 * - NaN/Inf サニタイ + 送信前の SHA-256 ロ
 * - text part の型安全生成
 */
class NetworkClient(
    private val context: Context, // <-- Context を受け取る
    private val baseUrl: String, // Store baseUrl as a class property
    okHttpClient: OkHttpClient? = null,
    userAgent: String = DEFAULT_UA,
    private val maxRetries: Int = 2,               // 合計 1 + 2 = 最大3回
    private val initialBackoffMs: Long = 5000L,     // リトライ初回待機: 5秒に延長
    // Backwards-compatible optional authToken: prefer this if provided; otherwise read from AppPreferences
    private val authToken: String? = null,
) {

    private val api: ApiService
    // Preserve the underlying client so we can issue raw downloads
    private val client: OkHttpClient

    // Common media types used by multipart helpers (defined early so helper methods can reference them)
    private val TEXT = "text/plain".toMediaType()
    private val OCTET = "application/octet-stream".toMediaType()

    // SharedPreferences への参照
    private val sharedPreferences: SharedPreferences =
        context.getSharedPreferences("send_history", Context.MODE_PRIVATE)

    // DB helper for persistent ack storage
    private val dbHelper: SendHistoryDbHelper = SendHistoryDbHelper(context)

    init {
        // initialize perf logger
        PerfLogger.init(context)

        // Determine effective auth token: prefer constructor arg, fallback to AppPreferences
        val prefsApp = context.getSharedPreferences("AppPreferences", Context.MODE_PRIVATE)
        val effectiveToken = if (!this.authToken.isNullOrBlank()) this.authToken else prefsApp.getString("authToken", null)

        if (!effectiveToken.isNullOrBlank()) {
            client = (okHttpClient ?: defaultOkHttpClient())
                .newBuilder()
                .addInterceptor(experimentHeaderInterceptor())
                .addInterceptor(uaInterceptor(userAgent))
                .addInterceptor(authInterceptor(effectiveToken))
                .build()
        } else {
            RealTimeLogger.w("NetworkClient", "authToken is null or blank. Skipping authInterceptor.")
            client = (okHttpClient ?: defaultOkHttpClient())
                .newBuilder()
                .addInterceptor(experimentHeaderInterceptor())
                .addInterceptor(uaInterceptor(userAgent))
                .build()
        }

        api = Retrofit.Builder()
            .baseUrl(ensureEndsWithSlash(baseUrl))
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(ApiService::class.java)
    }

    // 実験追跡ヘッダを全リクエストに付与（サーバーログとの突き合わせ用）
    private fun experimentHeaderInterceptor() = Interceptor { chain ->
        val req = chain.request().newBuilder()
            .addHeader("X-Run-Id", com.example.hfl_experiment.experiment.ExperimentContext.runId.ifEmpty { "none" })
            .addHeader("X-Terminal-Id", com.example.hfl_experiment.experiment.ExperimentContext.terminalId.ifEmpty { "unknown" })
            .addHeader("X-Round-Id", com.example.hfl_experiment.experiment.ExperimentContext.currentRound.toString())
            .build()
        chain.proceed(req)
    }

    // Adds Authorization: Bearer <token> header when token is present
    private fun authInterceptor(token: String) = Interceptor { chain ->
        val req = chain.request().newBuilder()
            .addHeader("Authorization", "Bearer $token")
            .build()
        chain.proceed(req)
    }

    // region --- Public APIs ---

    // キャッシュ用の変数
    private var cachedMeta: MetaResp? = null
    private var lastFetchMetaTime: Long = 0L
    private val cacheDurationMs: Long = 60 * 1000 // 1分間キャッシュ
    // current in-flight fetch meta deferred (to coalesce concurrent HTTP requests)
    private var inFlightMetaRequest: CompletableDeferred<MetaResp>? = null

    // Client-side throttle to avoid emitting multiple /api/v1/meta HTTP requests in a short burst
    // (per-instance) we still keep cachedMeta etc, but the throttle tracking is shared in companion object
    /** サーバのメタ情報を取得（キャッシュ対応版）。
     *  @param forceRefresh true の場合はキャッシュを無視して常にサーバに問い合わせる
     *  @param ignoreThrottle true の場合はスロットリング制御を無視して強制的にHTTPリクエストを行う
     */
    suspend fun fetchMeta(forceRefresh: Boolean = false, ignoreThrottle: Boolean = false): MetaResp = withContext(Dispatchers.IO) {
        val currentTime = System.currentTimeMillis()

        // Fast path: return cached if allowed
        if (!forceRefresh && cachedMeta != null && (currentTime - lastFetchMetaTime) < cacheDurationMs) {
            RealTimeLogger.d(TAG, "fetchMeta: Returning cached meta data.")
            return@withContext cachedMeta!!
        }

        // If there's already an in-flight fetch, wait for it and return result to avoid duplicate HTTP calls
        val existing = synchronized(metaRequestLock) { inFlightMetaRequest }
        if (existing != null) {
            try {
                RealTimeLogger.d(TAG, "fetchMeta: waiting for existing in-flight meta request to complete")
                val res = existing.await()
                return@withContext res
            } catch (e: Exception) {
                // fall through to attempt a new request
                RealTimeLogger.w(TAG, "fetchMeta: existing in-flight meta request failed: ${e.message}")
            }
        }

        // Throttle guard: if forceRefresh requested too soon after last actual HTTP, return cached if available
        if (!ignoreThrottle) {
            val elapsedSinceLastRequest: Long = synchronized(metaRequestLock) { currentTime - lastMetaRequestTs }
            if (forceRefresh && elapsedSinceLastRequest < MIN_META_REQUEST_INTERVAL_MS && cachedMeta != null) {
                RealTimeLogger.w(TAG, "fetchMeta: forceRefresh requested but last meta HTTP request was ${elapsedSinceLastRequest}ms ago; returning cached to avoid burst")
                return@withContext cachedMeta!!
            }
        } else {
            RealTimeLogger.i(TAG, "fetchMeta: ignoreThrottle=true, skipping throttle check")
        }

        // Create an in-flight deferred so other callers will wait for this HTTP call instead of issuing their own
        val deferred = CompletableDeferred<MetaResp>()
        synchronized(metaRequestLock) {
            inFlightMetaRequest = deferred
            lastMetaRequestTs = currentTime
        }

        try {
            val startTime = System.currentTimeMillis()
            val meta = retrying("fetchMeta") { api.fetchMeta() }
            val endTime = System.currentTimeMillis()
            RealTimeLogger.i(TAG, "fetchMeta: Request completed in ${endTime - startTime} ms forceRefresh=$forceRefresh")

            // update cache
            cachedMeta = meta
            lastFetchMetaTime = System.currentTimeMillis()

            // complete deferred so waiters receive the result
            deferred.complete(meta)
            return@withContext meta
        } catch (e: Exception) {
            deferred.completeExceptionally(e)
            throw e
        } finally {
            synchronized(metaRequestLock) { inFlightMetaRequest = null }
        }
    }

    /** 端末向けの配布内容を取得。 */
    suspend fun fetchSendToDevice(okHttpClient: OkHttpClient? = null): SendToDeviceDto = withContext(Dispatchers.IO) {
        val startTs = System.currentTimeMillis()
        RealTimeLogger.i(TAG, "fetchSendToDevice: ENTER baseUrl=${ensureEndsWithSlash(baseUrl)} useCustomClient=${okHttpClient != null}")
        try {
            // Build an OkHttp client with enforced short timeouts to prevent indefinite hangs
            val effectiveClient = (okHttpClient ?: client).newBuilder()
                .connectTimeout(5, TimeUnit.SECONDS)
                .readTimeout(10, TimeUnit.SECONDS)
                .writeTimeout(10, TimeUnit.SECONDS)
                .callTimeout(35, TimeUnit.SECONDS) // slightly above individual timeouts
                .build()

            // Build a temporary Retrofit that uses the effective client
            val retrofitTmp = Retrofit.Builder()
                .baseUrl(ensureEndsWithSlash(baseUrl))
                .client(effectiveClient)
                .addConverterFactory(GsonConverterFactory.create())
                .build()
            val apiTmp = retrofitTmp.create(ApiService::class.java)

            RealTimeLogger.i(TAG, "fetchSendToDevice: calling API with enforced timeouts (connect=5s, read=10s, call=35s)")

            // Primary attempt: try the Retrofit API call but bound by a coroutine timeout as extra safety
            val primaryDto: SendToDeviceDto? = try {
                // use withTimeout so we never wait forever even if underlying libs misbehave
                withTimeout(36_000L) { // 36s safety margin
                    retrying("sendToDevice") { apiTmp.fetchSendToDevice() }
                }
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "fetchSendToDevice: primary Retrofit call failed: ${e.message}")
                null
            }

            if (primaryDto != null) {
                RealTimeLogger.i(TAG, "fetchSendToDevice: received dto in ${System.currentTimeMillis() - startTs}ms (primary)")
                try { logSendToDeviceDto(primaryDto) } catch (_: Exception) {}
                return@withContext primaryDto
            }

            // Fallback: Try raw GET to common endpoints to tolerate server routing differences
            val fallbackPaths = listOf("send_to_device", "api/v1/send_to_device")
            for (p in fallbackPaths) {
                val url = ensureEndsWithSlash(baseUrl) + p
                RealTimeLogger.i(TAG, "fetchSendToDevice: attempting fallback GET url=$url")
                try {
                    val req = Request.Builder().url(url).get().build()
                    effectiveClient.newCall(req).execute().use { resp ->
                        val code = resp.code
                        val bodyStr = try { resp.body?.string() } catch (e: Exception) {
                            RealTimeLogger.w(TAG, "fetchSendToDevice: failed to read body for $url: ${e.message}")
                            null
                        }
                        if (resp.isSuccessful && !bodyStr.isNullOrBlank()) {
                            // Parse JSON into DTO using Gson
                            try {
                                val gson = com.google.gson.Gson()
                                val parsed = gson.fromJson(bodyStr, SendToDeviceDto::class.java)
                                RealTimeLogger.i(TAG, "fetchSendToDevice: fallback GET succeeded url=$url code=$code bytes=${bodyStr.length}")
                                try { logSendToDeviceDto(parsed) } catch (_: Exception) {}
                                return@withContext parsed
                            } catch (e: Exception) {
                                RealTimeLogger.w(TAG, "fetchSendToDevice: JSON parse failed for $url: ${e.message}")
                            }
                        } else {
                            RealTimeLogger.w(TAG, "fetchSendToDevice: fallback GET non-success url=$url code=$code bodyPreview=${bodyStr?.take(300)}")
                        }
                    }
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "fetchSendToDevice: fallback GET attempt to $url failed: ${e.message}")
                }
            }

            // If we reach here, all attempts failed
            throw IOException("fetchSendToDevice: all attempts failed (primary + fallback)")
        } catch (e: Exception) {
            RealTimeLogger.e(TAG, "fetchSendToDevice: FAILED after ${System.currentTimeMillis() - startTs}ms err=${e.message}", e)
            throw e
        }
    }

    // helper to log SendToDeviceDto in concise, non-sensitive way
    private fun logSendToDeviceDto(dto: SendToDeviceDto) {
        try {
            val sb = StringBuilder()
            sb.append("SendToDeviceDto: ")
            dto.paths?.let { p -> sb.append("paths={")
                if (!p.data_rel.isNullOrBlank()) sb.append("data_rel=${p.data_rel};")
                if (!p.model_rel.isNullOrBlank()) sb.append("model_rel=${p.model_rel};")
                sb.append("}")
            }
            dto.links?.let { l -> sb.append(" links={")
                if (!l.data.isNullOrBlank()) sb.append("data=${l.data};")
                if (!l.model.isNullOrBlank()) sb.append("model=${l.model};")
                sb.append("}")
            }
            // Backward/alternate fields: model/data/config entries
            dto.data?.let { d -> sb.append(" data={")
                if (!d.path.isNullOrBlank()) sb.append("path=${d.path};")
                if (!d.media_type.isNullOrBlank()) sb.append("media_type=${d.media_type};")
                d.status_code?.let { sb.append("status=${it};") }
                sb.append("}")
            }
            dto.model?.let { m -> sb.append(" model={")
                if (!m.path.isNullOrBlank()) sb.append("path=${m.path};")
                if (!m.media_type.isNullOrBlank()) sb.append("media_type=${m.media_type};")
                m.status_code?.let { sb.append("status=${it};") }
                sb.append("}")
            }
            dto.config?.let { c -> sb.append(" config={")
                c.status_code?.let { sb.append("status=${it};") }
                sb.append("}")
            }
            RealTimeLogger.i(TAG, sb.toString())
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "logSendToDeviceDto failed: ${e.message}")
        }
    }

    // 新規: askモードや短時間取得のためのタイムアウト付きヘルパー
    suspend fun fetchSendToDeviceShort(timeoutMs: Long = 5000L, reqId: String? = null): SendToDeviceDto? = withContext(Dispatchers.IO) {
        val startTs = System.currentTimeMillis()
        RealTimeLogger.i(TAG, "fetchSendToDeviceShort: ENTER reqId=${reqId} timeoutMs=${timeoutMs}")
        try {
            // Build a client with an explicit call timeout so the OkHttp call will be cancelled
            val clientShort = client.newBuilder()
                .callTimeout(timeoutMs, TimeUnit.MILLISECONDS)
                .build()

            // Build a retrofit instance that reuses the same converters but uses the short-timeout client
            val retrofitShort = Retrofit.Builder()
                .baseUrl(ensureEndsWithSlash(baseUrl))
                .client(clientShort)
                .addConverterFactory(GsonConverterFactory.create())
                .build()

            val apiShort = retrofitShort.create(ApiService::class.java)

            // Optionally attach a req-id header by doing a direct call via OkHttp if needed.
            // For simplicity, call the API as usual; caller can pass reqId which will be logged locally.
            val start = System.currentTimeMillis()
            val resp = try {
                apiShort.fetchSendToDevice()
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "fetchSendToDeviceShort: fetch failed reqId=${reqId} err=${e.message}")
                throw e
            }
            val duration = System.currentTimeMillis() - start
            RealTimeLogger.i(TAG, "fetchSendToDeviceShort: succeeded reqId=${reqId} duration=${duration}ms")
            try { logSendToDeviceDto(resp) } catch (_: Exception) {}
            return@withContext resp
        } catch (e: Exception) {
            // Return null on failure so callers can treat short-timeout attempts as best-effort
            RealTimeLogger.w(TAG, "fetchSendToDeviceShort: failed/timeout reqId=${reqId} err=${e.message} elapsed=${System.currentTimeMillis()-startTs}ms")
            return@withContext null
        }
    }

    // 新規: サーバにラウンドリセットを要求するAPI
    suspend fun resetRound(): Boolean = withContext(Dispatchers.IO) {
        val start = System.currentTimeMillis()
        val resp = retrying("resetRound") { api.resetRound() }
        val duration = System.currentTimeMillis() - start
        if (!resp.isSuccessful) {
            RealTimeLogger.w(TAG, "resetRound failed: code=${resp.code()} duration=${duration}ms")
            // RetrofitのResponse型を元に例外を投げて呼び出し側で処理させる
            throw HttpException(resp as retrofit2.Response<Any?>)
        }
        RealTimeLogger.i(TAG, "resetRound: success code=${resp.code()} duration=${duration}ms")
        return@withContext true
    }

    /**
     * Download arbitrary URL (absolute) and return bytes.
     */
    @Suppress("unused")
    suspend fun downloadUrl(url: String): ByteArray = withContext(Dispatchers.IO) {
        // Implement explicit handling for 404 (retry few times with 1s,2s,4s) and 429 (respect Retry-After or wait a few seconds)
        val max404Retries = 3
        var attempt = 0
        var waitMsFor404 = 1000L

        while (true) {
            attempt++
            try {
                val clientWithTimeout = client.newBuilder()
                    // For diagnostic download, ensure connect/read/write timeouts are generous
                    .connectTimeout(60, TimeUnit.SECONDS)
                    .callTimeout(120, TimeUnit.SECONDS)
                    .readTimeout(120, TimeUnit.SECONDS)
                    .writeTimeout(120, TimeUnit.SECONDS)
                    .build()
                val req = Request.Builder().url(url).get().build()
                val start = System.currentTimeMillis()

                clientWithTimeout.newCall(req).execute().use { resp ->
                    val execEnd = System.currentTimeMillis()
                    RealTimeLogger.i(TAG, "downloadUrl: attempt=$attempt execute time=${execEnd - start} ms for url=$url")

                    if (resp.isSuccessful) {
                        val body = resp.body
                        val bodyBytes = try { body?.bytes() ?: ByteArray(0) } catch (e: Exception) {
                            RealTimeLogger.w(TAG, "downloadUrl: failed to read body bytes on attempt=$attempt: ${e.message}")
                            null
                        }
                        if (bodyBytes != null) {
                            RealTimeLogger.i(TAG, "downloadUrl: success bytes=${bodyBytes.size} attempt=$attempt url=$url headers=${resp.headers.toMultimap().entries.take(10).joinToString(";")}")
                            return@withContext bodyBytes
                        } else {
                            throw IOException("downloadUrl: empty body on successful response")
                        }
                    } else {
                        val code = resp.code
                        // try to preview body safely
                        val bodyPreview = try { resp.body?.string() } catch (_: Exception) { null }
                        RealTimeLogger.w(TAG, "downloadUrl: non-success http=$code attempt=$attempt url=$url bodyPreview=${bodyPreview?.take(200)} headers=${resp.headers.toMultimap().entries.take(10).joinToString(";")}")

                        // 404 retry logic: server may not have generated the file yet
                        if (code == 404 && attempt <= max404Retries) {
                            RealTimeLogger.i(TAG, "downloadUrl: received 404, will wait ${waitMsFor404}ms and retry (attempt=$attempt/$max404Retries)")
                            delay(waitMsFor404)
                            waitMsFor404 = (waitMsFor404 * 2).coerceAtMost(30_000L)
                            continue
                        }

                        // 429 handling: respect Retry-After header if present, otherwise wait a default small interval
                        if (code == 429) {
                            val retryAfterHeader = try { resp.header("Retry-After") } catch (_: Exception) { null }
                            val baseSec = retryAfterHeader?.toLongOrNull() ?: 3L
                            val waitMs = (baseSec * 1000L).coerceAtLeast(3000L)
                            RealTimeLogger.w(TAG, "downloadUrl: received 429 Too Many Requests, waiting ${waitMs}ms before retry (Retry-After=${retryAfterHeader})")
                            delay(waitMs)
                            continue
                        }

                        // For other non-success codes do not retry here; throw detailed exception
                        throw IOException("Failed to download $url: code=$code body=${bodyPreview?.take(1000)}")
                    }
                }
            } catch (ce: CancellationException) {
                throw ce
            } catch (e: Exception) {
                RealTimeLogger.e(TAG, "downloadUrl attempt $attempt exception for url=$url: ${e.message}", e)
                // If 404 retries remain, use the same 404 backoff path; otherwise, for network IO we give a small backoff before final failure
                if (attempt < max404Retries) {
                    RealTimeLogger.i(TAG, "downloadUrl: network exception, will wait ${waitMsFor404}ms and retry (attempt=$attempt)")
                    try { delay(waitMsFor404) } catch (ie: CancellationException) { throw ie }
                    waitMsFor404 = (waitMsFor404 * 2).coerceAtMost(30_000L)
                    continue
                }
                throw e
            }
        }
        // Safety fallback: if we ever exit the loop unexpectedly, throw explicit exception
        throw IOException("downloadUrl: exhausted retries for $url")
    }

    /**
     * 学習済みパラメータ（flat float32）を multipart で送信。
     * サーバの preferred_dtype == "f32_flat" のときに使用。
     */
    suspend fun uploadFlatWeights(
        terminalId: String,
        meta: MetaResp,
        nSamples: Int,
        flatF32: FloatArray,
        fileName: String = "weights.bin",
        payloadKind: String = "full",
        hiddenSize: Int,
        outputSize: Int,
        inputSize: Int,
        clientMetaJson: String? = null, // <-- optional client-side metadata JSON
        virtualRouterId: String? = null,
        progressCallback: ((sentBytes: Long, totalBytes: Long) -> Unit)? = null, // optional progress callback
        // Caller can request to skip fetching authoritative meta from server before upload.
        // Default false maintains backward compatibility.
        skipFetchMeta: Boolean = false
    ) = withContext(Dispatchers.IO) {
        RealTimeLogger.i(TAG, "uploadFlatWeights: ENTER terminalId=$terminalId nSamples=$nSamples inputSize=$inputSize hiddenSize=$hiddenSize outputSize=$outputSize skipFetchMeta=$skipFetchMeta flatLength=${flatF32.size}")
        // NaN/Inf サニタイズ
        for (i in flatF32.indices) if (!flatF32[i].isFinite()) flatF32[i] = 0f

        // Write flat array to temporary file in chunks to avoid large in-memory ByteArray
        val tmpFile = File.createTempFile("weights", ".bin", context.cacheDir)
        try {
            val writeStart = System.currentTimeMillis()
            // 改善: 書き込み中に SHA-256 を計算して返す（ファイルを再読しない）
            val sha256 = writeFloatArrayToFile(flatF32, tmpFile)
            val writeEnd = System.currentTimeMillis()
            val writeMs = writeEnd - writeStart
            RealTimeLogger.i(TAG, "uploadFlatWeights: wrote temp file size=${tmpFile.length()} bytes in $writeMs ms path=${tmpFile.absolutePath}")
            PerfLogger.append(TAG, "wrote temp weights file size=${tmpFile.length()} ms=$writeMs path=${tmpFile.absolutePath}")


            // Generate a request id for this attempt
            val reqId = UUID.randomUUID().toString()

            // Fetch authoritative meta from server (best-effort) and prefer it over caller-provided meta
            val authoritativeMeta = if (skipFetchMeta) {
                // Caller asked to skip server meta fetch; use provided meta as authoritative
                RealTimeLogger.i(TAG, "uploadFlatWeights: skipFetchMeta=true -> using caller-provided meta (round=${meta.round})")
                meta
            } else {
                try {
                    RealTimeLogger.i(TAG, "uploadFlatWeights: fetching authoritative meta from server before upload")
                    // ★修正: アップロード前はスロットリングを無視して確実に最新を取得する
                    fetchMeta(forceRefresh = true, ignoreThrottle = true)
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "uploadFlatWeights: failed to fetch authoritative meta; falling back to provided meta: ${e.message}")
                    meta
                }
            }

            // If local DB already has this sha, skip upload
            if (dbHelper.exists(sha256)) {
                RealTimeLogger.i(TAG, "uploadFlatWeights: sha already acked locally, skipping upload: $sha256")
                return@withContext
            }

            // Optional pre-check with server marker API
            try {
                val existsOnServer = checkServerMarker(sha256)
                if (existsOnServer) {
                    RealTimeLogger.i(TAG, "uploadFlatWeights: server marker reports already exists for $sha256; marking locally and skipping upload")
                    markAsSent(sha256, reqId, authoritativeMeta.round)
                    return@withContext
                }
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "uploadFlatWeights: marker check failed; proceeding with upload: ${e.message}")
            }

            val weightsPart = makeWeightsPartFromFile(tmpFile, fileName, progressCallback)
            val parts = makeTextParts(
                round = authoritativeMeta.round,
                modelId = authoritativeMeta.model_id,
                baseHash = authoritativeMeta.base_hash,
                nSamples = nSamples,
                payloadKind = payloadKind,
                dtype = "f32_flat",
                inputSize = inputSize,
                hiddenSize = hiddenSize,
                outputSize = outputSize,
                clientMetaJson = clientMetaJson, // 修正箇所
                contentSha256 = sha256, // 修正箇所
                reqId = reqId
                , virtualRouterId = virtualRouterId
            )

            // Ensure compatibility: some server implementations expect camelCase key `contentSha256`.
            // If our map doesn't include camelCase, add it as duplicate for safety.
            if (!parts.containsKey("contentSha256") && sha256.isNotBlank()) {
                // makeTextParts returns an immutable Map backed by buildMap; we create a mutable copy to add the camelCase alias
                val mutable = parts.toMutableMap()
                mutable["contentSha256"] = sha256.toRequestBody(TEXT)
                // replace parts reference
                // NOTE: using partsRef below
                // shadow variable: partsRef
                val partsRef = mutable.toMap()
                // use partsRef from here on

                // Build the request with partsRef
                val requestForLog = makeMultipartRequest("/receive_terminal_weights/$terminalId", partsRef, weightsPart)
                RealTimeLogger.i(TAG, "uploadFlatWeights: Prepared multipart request to url=${requestForLog.url} tmpFile=${tmpFile.absolutePath} size=${tmpFile.length()} bytes parts=${partsRef.keys}")

                // Record a CSV log entry (START) so operator can pull device logs later
                try {
                    appendUploadCsvStatus(
                        reqId = reqId,
                        terminalId = terminalId,
                        url = requestForLog.url.toString(),
                        filePath = tmpFile.absolutePath,
                        fileSize = tmpFile.length(),
                        sha256 = sha256,
                        round = authoritativeMeta.round,
                        status = "START"
                    )
                } catch (_: Throwable) { }

                // Execute with retrying wrapper but provide more detailed logs around execution
                retrying("uploadWeights(f32_flat)") {
                    val reqStart = System.currentTimeMillis()
                    val resp = try { executeRequestWithDebug(client, requestForLog) } catch (e: Exception) {
                        RealTimeLogger.e(TAG, "uploadFlatWeights: request execution threw: ${e.message}", e)
                        // record failure in CSV
                        try {
                            appendUploadCsvStatus(reqId, terminalId, requestForLog.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "FAIL", null, System.currentTimeMillis() - reqStart, e.message)
                        } catch (_: Throwable) { }
                        throw e
                    }
                    val reqEnd = System.currentTimeMillis()
                    RealTimeLogger.i(TAG, "uploadFlatWeights: request executed in ${reqEnd - reqStart} ms for terminalId=$terminalId url=${requestForLog.url}")
                    PerfLogger.append(TAG, "upload request executed ms=${reqEnd - reqStart} terminalId=$terminalId url=${requestForLog.url}")

                    val bodyStr = resp.body.string()
                    if (!resp.isSuccessful) {
                        RealTimeLogger.e(TAG, "uploadFlatWeights: server returned non-success code=${resp.code} body=$bodyStr")
                        // Detect duplicate/ignored scenarios in non-200 responses (server may return 400 with "weights size mismatch" or include "duplicate")
                        val lc = bodyStr.lowercase()
                        val isDuplicateResponse = (resp.code == 400 && lc.contains("weights size mismatch")) || lc.contains("duplicate") || lc.contains("duplicate_ignored")
                        if (isDuplicateResponse) {
                            RealTimeLogger.i(TAG, "uploadFlatWeights: server indicates duplicate/ignored; treating as ACK/success (code=${resp.code}) body=$bodyStr")
                            // record success/ack and mark as sent
                            try { appendUploadCsvStatus(reqId, terminalId, requestForLog.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "SUCCESS_DUPLICATE", resp.code, reqEnd - reqStart, bodyStr) } catch (_: Throwable) { }
                            markAsSent(sha256, reqId, authoritativeMeta.round)
                            // return a synthetic JSON object indicating ack/duplicate so the caller treats as success
                            val jo = JSONObject()
                            jo.put("ack", true)
                            jo.put("status", "duplicate_ignored")
                            jo.put("detail", bodyStr)
                            return@retrying jo
                        }
                        // otherwise record failure and throw to retry
                        try { appendUploadCsvStatus(reqId, terminalId, requestForLog.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "FAIL", resp.code, reqEnd - reqStart, bodyStr) } catch (_: Throwable) { }
                        throw IOException("Upload failed http=${resp.code} body=$bodyStr")
                    }

                    if (bodyStr.isBlank()) throw IOException("Empty response body")

                    val json = JSONObject(bodyStr)
                    val ack = json.optBoolean("ack", false)
                    val status = json.optString("status", "")
                    val duplicate = status == "duplicate_ignored" || json.optString("detail", "") == "duplicate_ignored"

                    RealTimeLogger.i(TAG, "uploadFlatWeights: server response body=$bodyStr")

                    if (!ack && !duplicate) {
                        // record failure (no ack)
                        try { appendUploadCsvStatus(reqId, terminalId, requestForLog.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "FAIL", resp.code, reqEnd - reqStart, "no_ack") } catch (_: Throwable) { }
                        throw IOException("No ack from server; body=$bodyStr")
                    }

                    val ackTs = json.optString("ack_timestamp", "")
                    val serverSha = json.optString("sha256", "")
                    RealTimeLogger.i(TAG, "Upload OK: ack=$ack ack_ts=$ackTs sha=$serverSha")

                    // Persist with sha + reqId (durable DB)
                    markAsSent(sha256, reqId, authoritativeMeta.round, ackTs)

                    // record success
                    try { appendUploadCsvStatus(reqId, terminalId, requestForLog.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "SUCCESS", resp.code, reqEnd - reqStart, "ack=$ack") } catch (_: Throwable) { }

                    return@retrying json
                }
            }

            // If we fell through (no camelCase injection branch), proceed with original parts map

            // normal path: log request details
            val requestNormal = makeMultipartRequest("/receive_terminal_weights/$terminalId", parts, weightsPart)
            RealTimeLogger.i(TAG, "uploadFlatWeights: Prepared multipart request to url=${requestNormal.url} tmpFile=${tmpFile.absolutePath} size=${tmpFile.length()} bytes parts=${parts.keys}")

            // Record CSV START for normal path
            try {
                appendUploadCsvStatus(reqId = reqId, terminalId = terminalId, url = requestNormal.url.toString(), filePath = tmpFile.absolutePath, fileSize = tmpFile.length(), sha256 = sha256, round = authoritativeMeta.round, status = "START")
            } catch (_: Throwable) { }

            retrying("uploadWeights(f32_flat)") {
                val reqStart = System.currentTimeMillis()
                val resp = try { executeRequestWithDebug(client, requestNormal) } catch (e: Exception){
                    RealTimeLogger.e(TAG, "uploadFlatWeights: request execution threw: ${e.message}", e)
                    try { appendUploadCsvStatus(reqId, terminalId, requestNormal.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "FAIL", null, System.currentTimeMillis() - reqStart, e.message) } catch (_: Throwable) { }
                    throw e
                }
                val reqEnd = System.currentTimeMillis()
                RealTimeLogger.i(TAG, "uploadFlatWeights: request executed in ${reqEnd - reqStart} ms for terminalId=$terminalId url=${requestNormal.url}")
                PerfLogger.append(TAG, "upload request executed ms=${reqEnd - reqStart} terminalId=$terminalId url=${requestNormal.url}")

                val bodyStr = resp.body.string()
                if (!resp.isSuccessful) {
                    RealTimeLogger.e(TAG, "uploadFlatWeights: server returned non-success code=${resp.code} body=$bodyStr")
                    val lc = bodyStr.lowercase()
                    val isDuplicateResponse = (resp.code == 400 && lc.contains("weights size mismatch")) || lc.contains("duplicate") || lc.contains("duplicate_ignored")
                    if (isDuplicateResponse) {
                        RealTimeLogger.i(TAG, "uploadFlatWeights: server indicates duplicate/ignored; treating as ACK/success (code=${resp.code}) body=$bodyStr")
                        try { appendUploadCsvStatus(reqId, terminalId, requestNormal.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "SUCCESS_DUPLICATE", resp.code, reqEnd - reqStart, bodyStr) } catch (_: Throwable) { }
                        markAsSent(sha256, reqId, authoritativeMeta.round)
                        val jo = JSONObject()
                        jo.put("ack", true)
                        jo.put("status", "duplicate_ignored")
                        jo.put("detail", bodyStr)
                        return@retrying jo
                    }
                    try { appendUploadCsvStatus(reqId, terminalId, requestNormal.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "FAIL", resp.code, reqEnd - reqStart, bodyStr) } catch (_: Throwable) { }
                    throw IOException("Upload failed http=${resp.code} body=$bodyStr")
                }


                if (bodyStr.isBlank()) throw IOException("Empty response body")

                val json = JSONObject(bodyStr)
                val ack = json.optBoolean("ack", false)
                val status = json.optString("status", "")
                val duplicate = status == "duplicate_ignored" || json.optString("detail", "") == "duplicate_ignored"

                RealTimeLogger.i(TAG, "uploadFlatWeights: server response body=$bodyStr")

                if (!ack && !duplicate) {
                    try { appendUploadCsvStatus(reqId, terminalId, requestNormal.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "FAIL", resp.code, reqEnd - reqStart, "no_ack") } catch (_: Throwable) { }
                    throw IOException("No ack from server; body=$bodyStr")
                }

                val ackTs = json.optString("ack_timestamp", "")
                val serverSha = json.optString("sha256", "")
                RealTimeLogger.i(TAG, "Upload OK: ack=$ack ack_ts=$ackTs sha=$serverSha")

                // Persist with sha + reqId (durable DB)
                markAsSent(sha256, reqId, authoritativeMeta.round, ackTs)

                // Record success
                try { appendUploadCsvStatus(reqId, terminalId, requestNormal.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "SUCCESS", resp.code, reqEnd - reqStart, "ack=$ack") } catch (_: Throwable) { }

                return@retrying json
            }
        } finally {
            try { tmpFile.delete() } catch (_: Throwable) { }
        } // end try/finally for tmpFile
    } // end withContext for uploadFlatWeights

    // New: write ModelParameters directly to file with SHA-256 and upload (memory-efficient)
    suspend fun uploadModelParameters(
        terminalId: String,
        meta: MetaResp,
        nSamples: Int,
        params: com.example.hfl_experiment.training.core.ModelParameters,
        fileName: String = "weights.bin",
        payloadKind: String = "full",
        inputSize: Int,
        hiddenSize: Int,
        outputSize: Int,
        clientMetaJson: String? = null,
        virtualRouterId: String? = null,
        progressCallback: ((sentBytes: Long, totalBytes: Long) -> Unit)? = null,
        skipFetchMeta: Boolean = false
    ) = withContext(Dispatchers.IO) {
        RealTimeLogger.i(TAG, "uploadModelParameters: ENTER terminalId=$terminalId nSamples=$nSamples inputSize=$inputSize hiddenSize=$hiddenSize outputSize=$outputSize skipFetchMeta=$skipFetchMeta")
        // Stream params to a temp file and compute sha256 while writing
        val tmpFile = File.createTempFile("weights", ".bin", context.cacheDir)
        try {
            val digest = MessageDigest.getInstance("SHA-256")
            FileOutputStream(tmpFile).use { fos ->
                // Helper to write a float in little-endian
                fun writeFloatLE(f: Float) {
                    val bb = ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putFloat(f)
                    val bytes = bb.array()
                    fos.write(bytes)
                    digest.update(bytes)
                }
                // weights1: List<List<Float>>
                for (row in params.weights1) for (v in row) writeFloatLE(v)
                // biases1
                for (v in params.biases1) writeFloatLE(v)
                // norm1 gamma/beta
                for (v in params.norm1Gamma) writeFloatLE(v)
                for (v in params.norm1Beta) writeFloatLE(v)
                // weights2
                for (row in params.weights2) for (v in row) writeFloatLE(v)
                // biases2
                for (v in params.biases2) writeFloatLE(v)
                // norm2 gamma/beta
                for (v in params.norm2Gamma) writeFloatLE(v)
                for (v in params.norm2Beta) writeFloatLE(v)
                // weights3
                for (row in params.weights3) for (v in row) writeFloatLE(v)
                // biases3
                for (v in params.biases3) writeFloatLE(v)
                fos.flush()
            }
            val shaBytes = digest.digest()
            val sha256 = shaBytes.joinToString("") { "%02x".format(it) }
            RealTimeLogger.i(TAG, "uploadModelParameters: wrote temp file size=${tmpFile.length()} bytes path=${tmpFile.absolutePath} sha=$sha256")

            // Generate reqId
            val reqId = UUID.randomUUID().toString()

            // authoritative meta handling (same as uploadFlatWeights)
            val authoritativeMeta = if (skipFetchMeta) {
                RealTimeLogger.i(TAG, "uploadModelParameters: skipFetchMeta=true -> using caller-provided meta (round=${meta.round})")
                meta
            } else {
                try { 
                    // ★修正: アップロード前はスロットリングを無視して確実に最新を取得する
                    fetchMeta(forceRefresh = true, ignoreThrottle = true) 
                } catch (e: Exception) { 
                    RealTimeLogger.w(TAG, "uploadModelParameters: fetchMeta failed, falling back: ${e.message}")
                    meta 
                }
            }

            // Check local DB marker
            if (dbHelper.exists(sha256)) {
                RealTimeLogger.i(TAG, "uploadModelParameters: sha already acked locally, skipping upload: $sha256")
                return@withContext
            }
            try {
                val existsOnServer = checkServerMarker(sha256)
                if (existsOnServer) {
                    RealTimeLogger.i(TAG, "uploadModelParameters: server marker reports already exists for $sha256; marking locally and skipping upload")
                    markAsSent(sha256, reqId, authoritativeMeta.round)
                    return@withContext
                }
            } catch (e: Exception) { RealTimeLogger.w(TAG, "uploadModelParameters: marker check failed; proceeding: ${e.message}") }

            val weightsPart = makeWeightsPartFromFile(tmpFile, fileName, progressCallback)
            val parts = makeTextParts(
                round = authoritativeMeta.round,
                modelId = authoritativeMeta.model_id,
                baseHash = authoritativeMeta.base_hash,
                nSamples = nSamples,
                payloadKind = payloadKind,
                dtype = "f32_flat",
                inputSize = inputSize,
                hiddenSize = hiddenSize,
                outputSize = outputSize,
                clientMetaJson = clientMetaJson,
                contentSha256 = sha256,
                reqId = reqId
                , virtualRouterId = virtualRouterId
             )

            // Ensure camelCase alias
            val partsRef = if (!parts.containsKey("contentSha256")) {
                val m = parts.toMutableMap()
                m["contentSha256"] = sha256.toRequestBody(TEXT)
                m.toMap()
            } else parts

            val request = makeMultipartRequest("/receive_terminal_weights/$terminalId", partsRef, weightsPart)
            RealTimeLogger.i(TAG, "uploadModelParameters: Prepared multipart request url=${request.url} tmpFile=${tmpFile.absolutePath} size=${tmpFile.length()} parts=${partsRef.keys}")

            try { appendUploadCsvStatus(reqId, terminalId, request.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "START") } catch (_: Throwable) {}

            try { RealTimeLogger.d("HFL_DEBUG", "UPLOAD_FLOW: POST_START reqId=${reqId} terminalId=${terminalId} url=${request.url}")
                } catch (_: Throwable) {}

            retrying("uploadModelParameters") {
                val start = System.currentTimeMillis()
                val resp = try { executeRequestWithDebug(client, request) } catch (e: Exception) {
                    try { RealTimeLogger.e("HFL_DEBUG", "UPLOAD_FAILED: executeRequest threw reqId=${reqId} err=${e::class.java.simpleName} ${e.message}") } catch (_: Throwable) {}
                    RealTimeLogger.e(TAG, "uploadModelParameters: request execution threw: ${e.message}", e)
                    try { appendUploadCsvStatus(reqId, terminalId, request.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "FAIL", null, System.currentTimeMillis() - start, e.message) } catch (_: Throwable) {}
                    throw e
                }
                val end = System.currentTimeMillis()
                val bodyStr = resp.body.string()
                if (!resp.isSuccessful) {
                    try { RealTimeLogger.e("HFL_DEBUG", "UPLOAD_FAILED: server returned non-success code=${resp.code} reqId=${reqId} body=${bodyStr.take(200)}") } catch (_: Throwable) {}
                    RealTimeLogger.e(TAG, "uploadModelParameters: server returned non-success code=${resp.code} body=$bodyStr")
                    try { appendUploadCsvStatus(reqId, terminalId, request.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "FAIL", resp.code, end - start, bodyStr) } catch (_: Throwable) {}

                    // 409 Conflict (round mismatch) -> force refresh meta and retry
                    if (resp.code == 409) {
                        RealTimeLogger.w(TAG, "uploadModelParameters: 409 Conflict detected. Forcing meta refresh and retrying.")
                        try {
                            fetchMeta(forceRefresh = true, ignoreThrottle = true) // 修正箇所: ignoreThrottle=trueを追加
                        } catch (e: Exception) {
                            RealTimeLogger.w(TAG, "uploadModelParameters: meta refresh failed during 409 handling: ${e.message}")
                        }
                        // Throw IOException to trigger the outer retrying loop
                        throw IOException("Upload failed with 409 Conflict (round mismatch)")
                    }

                    throw IOException("Upload failed http=${resp.code} body=$bodyStr")
                }
                if (bodyStr.isBlank()) throw IOException("Empty response body")

                val json = JSONObject(bodyStr)
                val ack = json.optBoolean("ack", false)
                val status = json.optString("status", "")
                val duplicate = status == "duplicate_ignored" || json.optString("detail", "") == "duplicate_ignored"

                RealTimeLogger.i(TAG, "uploadModelParameters: server response body=$bodyStr")

                if (!ack && !duplicate) {
                    try { appendUploadCsvStatus(reqId, terminalId, request.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "FAIL", resp.code, end - start, "no_ack") } catch (_: Throwable) { }
                    throw IOException("No ack from server; body=$bodyStr")
                }

                val ackTs = json.optString("ack_timestamp", "")
                val serverSha = json.optString("sha256", "")
                RealTimeLogger.i(TAG, "Upload OK: ack=$ack ack_ts=$ackTs sha=$serverSha")

                // Persist with sha + reqId (durable DB)
                markAsSent(sha256, reqId, authoritativeMeta.round, ackTs)

                // Record success
                try { appendUploadCsvStatus(reqId, terminalId, request.url.toString(), tmpFile.absolutePath, tmpFile.length(), sha256, authoritativeMeta.round, "SUCCESS", resp.code, end - start, "ack=$ack") } catch (_: Throwable) {}

                return@retrying json
            }
        } finally {
            try { tmpFile.delete() } catch (_: Throwable) {}
        }
    }

    /** Upload one or more client log files to server endpoint /upload_client_logs
     *  - files: map of fieldName->File
     *  - metaJson: optional metadata JSON (device id, round, timestamp)
     *  - returns server response body as String on success
     */
    suspend fun uploadClientLogs(
        terminalId: String,
        files: Map<String, File>,
        metaJson: String? = null,
        progressCallback: ((sentBytes: Long, totalBytes: Long) -> Unit)? = null
    ): String = withContext(Dispatchers.IO) {
        val boundary = "----HFLClientLogs${System.currentTimeMillis()}"
        val multipartBuilder = MultipartBody.Builder(boundary).setType(MultipartBody.FORM)
        val maxLogBytes = 10L * 1024 * 1024 // サーバー側の制限: 10MB
        var attached = 0
        files.forEach { entry ->
            val field = entry.key
            val file = entry.value
            if (!file.isFile || file.length() == 0L) {
                RealTimeLogger.w(TAG, "uploadClientLogs: skipping empty or missing ${file.name}")
                return@forEach
            }
            if (file.length() > maxLogBytes) {
                RealTimeLogger.w(TAG, "uploadClientLogs: skipping ${file.name} (${file.length()} bytes > 10MB limit)")
                return@forEach
            }
            try {
                val mediaType = "text/plain; charset=utf-8".toMediaType()
                multipartBuilder.addFormDataPart(field, file.name, file.asRequestBody(mediaType))
                attached++
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "uploadClientLogs: failed to attach file ${file.absolutePath}: ${e.message}")
            }
        }
        val isErrorReportMeta = try {
            !metaJson.isNullOrBlank() &&
                org.json.JSONObject(metaJson).optString("report_kind", "") == "error_report"
        } catch (_: Exception) {
            false
        }
        if (!metaJson.isNullOrBlank()) {
            multipartBuilder.addFormDataPart("meta", null, metaJson.toRequestBody("application/json; charset=utf-8".toMediaType()))
        }
        if (attached == 0 && !isErrorReportMeta) {
            throw IOException("uploadClientLogs: no non-empty files to upload")
        }
        if (attached == 0 && metaJson.isNullOrBlank()) {
            throw IOException("uploadClientLogs: meta JSON required for zero-file upload")
        }

        val body = multipartBuilder.build()

        val req = Request.Builder()
            .url(ensureEndsWithSlash(baseUrl) + "upload_client_logs/$terminalId")
            .post(body)
            .build()

        val start = System.currentTimeMillis()
        client.newCall(req).execute().use { resp ->
            val dur = System.currentTimeMillis() - start
            val code = resp.code
            if (!resp.isSuccessful) {
                val bodyStr = try { resp.body?.string() } catch (_: Throwable) { null }
                RealTimeLogger.w(TAG, "uploadClientLogs failed: code=$code duration=${dur}ms body=${bodyStr}")
                throw UploadClientLogsHttpException(code, bodyStr)
            }
            val respBody = try { resp.body?.string() } catch (_: Throwable) { "" } ?: ""
            RealTimeLogger.i(TAG, "uploadClientLogs: success duration=${dur}ms responseLen=${respBody.length}")
            return@withContext respBody
        }
    }

    /**
     * Send a single training metric line to server endpoint /upload_training_metrics.
     * Returns true on success (2xx), false on any failure.
     */
    suspend fun sendTrainingMetric(
        edgeId: String,
        round: Int,
        accuracy: Double,
        loss: Double,
        dataId: String? = null,
        eventTimestamp: String? = null,
        appType: String? = null,
        appIndex: Int? = null,
        satisfactionBefore: Double? = null,
        satisfactionAfter: Double? = null,
        tpMeasured: Double? = null,
        rttMeasured: Double? = null
    ): Boolean = withContext(Dispatchers.IO) {
        try {
            val jo = JSONObject()
            jo.put("terminal_id", edgeId)
            jo.put("round", round)
            jo.put("accuracy", accuracy)
            jo.put("loss", loss)
            dataId?.let { jo.put("data_id", it) }
            eventTimestamp?.let { jo.put("event_timestamp", it) }
            appType?.let { jo.put("app_type", it) }
            appIndex?.let { jo.put("app_index", it) }
            satisfactionBefore?.let { jo.put("satisfaction_before", it) }
            satisfactionAfter?.let { jo.put("satisfaction_after", it) }
            tpMeasured?.let { jo.put("tp_measured_mbps", it) }
            rttMeasured?.let { jo.put("rtt_measured_ms", it) }
            appType?.let { jo.put("app_type", it) }
            appIndex?.let { jo.put("app_index", it) }
            satisfactionBefore?.let { jo.put("satisfaction_before", it) }
            satisfactionAfter?.let { jo.put("satisfaction_after", it) }
            tpMeasured?.let { jo.put("tp_measured_mbps", it) }
            rttMeasured?.let { jo.put("rtt_measured_ms", it) }
            appType?.let { jo.put("app_type", it) }
            appIndex?.let { jo.put("app_index", it) }
            satisfactionBefore?.let { jo.put("satisfaction_before", it) }
            satisfactionAfter?.let { jo.put("satisfaction_after", it) }
            tpMeasured?.let { jo.put("tp_measured_mbps", it) }
            rttMeasured?.let { jo.put("rtt_measured_ms", it) }
            appType?.let { jo.put("app_type", it) }
            appIndex?.let { jo.put("app_index", it) }
            satisfactionBefore?.let { jo.put("satisfaction_before", it) }
            satisfactionAfter?.let { jo.put("satisfaction_after", it) }
            tpMeasured?.let { jo.put("tp_measured_mbps", it) }
            rttMeasured?.let { jo.put("rtt_measured_ms", it) }
            appType?.let { jo.put("app_type", it) }
            appIndex?.let { jo.put("app_index", it) }
            satisfactionBefore?.let { jo.put("satisfaction_before", it) }
            satisfactionAfter?.let { jo.put("satisfaction_after", it) }
            tpMeasured?.let { jo.put("tp_measured_mbps", it) }
            rttMeasured?.let { jo.put("rtt_measured_ms", it) }

            val jsonBody = jo.toString().toRequestBody("application/json".toMediaType())
            val url = ensureEndsWithSlash(baseUrl) + "upload_training_metrics"
            val req = Request.Builder()
                .url(url)
                .post(jsonBody)
                .build()

            client.newCall(req).execute().use { resp ->
                val bodyStr = try { resp.body.string() } catch (_: Throwable) { null }
                if (!resp.isSuccessful) {
                    RealTimeLogger.w(TAG, "sendTrainingMetric: server returned http=${resp.code} body=$bodyStr")
                    return@withContext false
                }
                RealTimeLogger.i(TAG, "sendTrainingMetric: success http=${resp.code} body=$bodyStr")
                return@withContext true
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "sendTrainingMetric: exception=${e.message}")
            return@withContext false
        }
    }

    // New helper: append a CSV line describing upload attempt/result for easy extraction via adb
    private fun appendUploadCsvStatus(
        reqId: String?,
        terminalId: String,
        url: String,
        filePath: String,
        fileSize: Long,
        sha256: String,
        round: Int?,
        status: String,
        httpCode: Int? = null,
        durationMs: Long? = null,
        message: String? = null
    ) {
        try {
            val logFile = File(context.filesDir, "upload_logs.csv")
            if (!logFile.exists()) {
                logFile.writeText("ts,req_id,terminal_id,url,file_path,file_size,sha256,round,status,http_code,duration_ms,message\n")
            }
            val ts = System.currentTimeMillis()
            val safeMsg = message?.replace("\n", " ")?.replace(",", ";")
            val line = listOf(ts, reqId ?: "", terminalId, url, filePath, fileSize.toString(), sha256, round?.toString() ?: "", status, httpCode?.toString() ?: "", durationMs?.toString() ?: "", safeMsg ?: "").joinToString(",") + "\n"
            logFile.appendText(line)
            // Also write an immediate logcat line so the upload appears in device logs in real-time
            try {
                RealTimeLogger.i("UPLOAD_CSV", line.trim())
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "Failed to write UPLOAD_CSV logcat line: ${e.message}")
            }
        } catch (e: Exception) {
            RealTimeLogger.w("NetworkClient", "appendUploadCsvStatus failed: ${e.message}")
        }
    }

    /**
     * Fetch OpenAPI/Swagger schema from server (best-effort). Returns raw JSON string or empty object on failure.
     */
    suspend fun fetchOpenApiSchema(): String = withContext(Dispatchers.IO) {
        val candidates = listOf("openapi.json", "openapi.yaml", "swagger.json")
        for (c in candidates) {
            try {
                val url = ensureEndsWithSlash(baseUrl) + c
                val req = Request.Builder().url(url).get().build()
                client.newCall(req).execute().use { resp ->
                    if (resp.isSuccessful) {
                        val body = resp.body.string()
                        if (!body.isNullOrBlank()) return@withContext body
                    }
                }
            } catch (_: Exception) { /* try next */ }
        }
        // fallback: return empty JSON object
        return@withContext "{}"
    }

    /**
     * Lightweight DTO vs OpenAPI schema consistency check.
     * This is a heuristic: if schema is blank we return false; otherwise we return true for simplicity.
     * Replace with a proper schema validator if stricter checking is required.
     */
    fun validateDtoConsistency(schemaStr: String, dto: Any): Boolean {
        if (schemaStr.isBlank() || schemaStr.trim() == "{}") return false
        // minimal heuristic: schema contains "properties" or "components"
        val lowered = schemaStr.lowercase()
        if (lowered.contains("properties") || lowered.contains("components") || lowered.contains("openapi")) return true
        return true
    }

    /**
     * Run quick network diagnostics: DNS lookup and TCP connect to baseUrl host:port
     * Returns a short text report suitable for logging.
     */
    suspend fun diagnoseAll(timeoutMs: Int = 3000): String = withContext(Dispatchers.IO) {
        val sb = StringBuilder()
        try {
            val uri = try { java.net.URI(baseUrl) } catch (e: Exception) { null }
            val host = uri?.host ?: try { java.net.URL(baseUrl).host } catch (_: Exception) { null }
            val port = when {
                uri != null && uri.port != -1 -> uri.port
                baseUrl.startsWith("https://") -> 443
                baseUrl.startsWith("http://") -> 80
                else -> 80
            }
            sb.append("diagnose_start ts=${System.currentTimeMillis()}\n")
            if (host == null) {
                sb.append("unable_to_parse_host_from_baseUrl=$baseUrl\n")
                return@withContext sb.toString()
            }
            sb.append("host=$host port=$port\n")
            // DNS
            try {
                val addrs = java.net.InetAddress.getAllByName(host).map { it.hostAddress }
                sb.append("dns_ok addresses=${addrs.joinToString(";")}\n")
            } catch (e: Exception) {
                sb.append("dns_fail err=${e.message}\n")
            }
            // TCP connect
            try {
                java.net.Socket().use { sock ->
                    sock.soTimeout = timeoutMs
                    val start = System.currentTimeMillis()
                    sock.connect(java.net.InetSocketAddress(host, port), timeoutMs)
                    val dur = System.currentTimeMillis() - start
                    sb.append("tcp_connect_ok duration_ms=$dur\n")
                }
            } catch (e: Exception) {
                sb.append("tcp_connect_fail err=${e.message}\n")
            }
        } catch (e: Exception) {
            sb.append("diagnose_exception ${e.message}\n")
        }
        sb.append("diagnose_end ts=${System.currentTimeMillis()}\n")
        return@withContext sb.toString()
    }

    /**
     * Measure RTT to the server (baseUrl or provided URL) by issuing lightweight HTTP HEAD requests
     * using OkHttp and measuring the call latency. Returns median RTT in milliseconds or null on failure.
     */
    suspend fun measureRtt(targetUrl: String? = null, attempts: Int = 3, perAttemptTimeoutMs: Int = 3000): Float? = withContext(Dispatchers.IO) {
        val urlToUse = try {
            if (targetUrl.isNullOrBlank()) ensureEndsWithSlash(baseUrl) else targetUrl
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "measureRtt: invalid targetUrl: ${e.message}")
            return@withContext null
        }

        val results = mutableListOf<Long>()
        for (i in 1..attempts) {
            try {
                val clientTmp = client.newBuilder()
                    .connectTimeout(perAttemptTimeoutMs.toLong(), TimeUnit.MILLISECONDS)
                    .readTimeout(perAttemptTimeoutMs.toLong(), TimeUnit.MILLISECONDS)
                    .writeTimeout(perAttemptTimeoutMs.toLong(), TimeUnit.MILLISECONDS)
                    .callTimeout((perAttemptTimeoutMs + 1000).toLong(), TimeUnit.MILLISECONDS)
                    .build()

                val req = Request.Builder().url(urlToUse).head().build()
                val start = System.currentTimeMillis()
                clientTmp.newCall(req).execute().use { resp ->
                    val end = System.currentTimeMillis()
                    val dur = end - start
                    // log any debug header that signals server-side injected delay
                    try {
                        val injected = resp.header("X-Injected-Delay-ms")
                        if (!injected.isNullOrBlank()) RealTimeLogger.i(TAG, "measureRtt: detected server header X-Injected-Delay-ms=$injected")
                    } catch (_: Exception) {}
                    RealTimeLogger.i(TAG, "measureRtt: attempt=$i url=$urlToUse code=${resp.code} duration_ms=${dur}")
                    if (resp.isSuccessful) results.add(dur) else {
                        // still record duration for inference
                        results.add(dur)
                    }
                }
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "measureRtt: attempt=$i failed: ${e.message}")
            }
            // small jitter between attempts
            try { kotlinx.coroutines.delay(150L) } catch (_: Exception) {}
        }

        if (results.isEmpty()) {
            RealTimeLogger.w(TAG, "measureRtt: no successful attempts to $urlToUse")
            return@withContext null
        }

        // compute median
        val sorted = results.sorted()
        val median = if (sorted.size % 2 == 1) sorted[sorted.size/2].toFloat() else ((sorted[sorted.size/2 -1] + sorted[sorted.size/2]) / 2f)
        RealTimeLogger.i(TAG, "measureRtt: median_ms=$median samples=${sorted.size}")
        return@withContext median
    }

    /**
     * Diagnose whether server-side artificial delay is present by comparing HTTP RTT to local gateway ping.
     * Returns a short textual report and logs details.
     */
    suspend fun diagnoseServerDelay(attempts: Int = 3, httpTimeoutMs: Int = 3000): String = withContext(Dispatchers.IO) {
        val report = StringBuilder()
        try {
            val serverRtt = try { measureRtt(null, attempts, httpTimeoutMs) } catch (e: Exception) { null }
            report.append("server_rtt_ms=${serverRtt ?: "null"}\n")

            // Attempt to get gateway RTT using local ping util (best-effort)
            val gatewayRtt = try {
                com.example.hfl_experiment.network.util.PingUtil.pingGateway(context.applicationContext)
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "diagnoseServerDelay: gateway ping failed: ${e.message}")
                null
            }
            report.append("gateway_rtt_ms=${gatewayRtt ?: "null"}\n")

            if (serverRtt != null && gatewayRtt != null) {
                val diff = serverRtt - gatewayRtt
                report.append("rtt_diff_ms=${diff}\n")
                val likelyServerDelay = diff > 50 // heuristic: >50ms indicates possible server-side delay
                report.append("likely_server_delay=${likelyServerDelay}\n")
            } else {
                report.append("insufficient_data_for_delay_inference\n")
            }
        } catch (e: Exception) {
            report.append("diagnose_exception=${e.message}\n")
        }
        val s = report.toString()
        RealTimeLogger.i(TAG, "diagnoseServerDelay report:\n$s")
        return@withContext s
    }

    fun shutdown() {
        try { client.dispatcher.executorService.shutdown() } catch (_: Exception) {}
        try { client.connectionPool.evictAll() } catch (_: Exception) {}
    }

    // ...existing code...
    companion object {
        private const val TAG = "NetworkClient"
        private const val DEFAULT_UA = "HFL-Android/1.0 (NetworkClient)"
        // Shared throttle state across NetworkClient instances
        internal val metaRequestLock = Any()
        internal var lastMetaRequestTs: Long = 0L
        internal const val MIN_META_REQUEST_INTERVAL_MS: Long = 5_000L // 5s minimum between actual HTTP meta requests across instances
    }

    // Minimal helper: ensure base URL ends with slash
    private fun ensureEndsWithSlash(u: String): String = if (u.endsWith("/")) u else "$u/"

    // Minimal default OkHttpClient builder used when none provided
    private fun defaultOkHttpClient(): OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(60, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    // User-Agent interceptor
    private fun uaInterceptor(ua: String) = Interceptor { chain ->
        val req: Request = chain.request().newBuilder()
            .header("User-Agent", ua)
            .build()
        chain.proceed(req)
    }

    // Simple retrying wrapper with exponential backoff. Uses class fields maxRetries/initialBackoffMs.
    private suspend fun <T> retrying(op: String, block: suspend () -> T): T {
        var attempt = 0
        var delayMs = initialBackoffMs
        var lastEx: Throwable? = null
        while (true) {
            try {
                return block()
            } catch (ce: CancellationException) {
                throw ce
            } catch (e: Throwable) {
                lastEx = e
                // Special handling for HTTP 429: respect Retry-After header if present and apply exponential backoff
                if (e is HttpException && e.code() == 429) {
                    val resp = e.response()
                    val retryAfterHeader = resp?.headers()?.get("Retry-After")
                    val baseSec = retryAfterHeader?.toLongOrNull() ?: (initialBackoffMs.coerceAtLeast(3000L) / 1000L)
                    val waitMs = (baseSec * 1000L) * (1L shl (attempt - 1)).coerceAtMost(60_000L)
                    if (attempt > maxRetries) break
                    try {
                        RealTimeLogger.w(TAG, "retrying($op): HTTP 429 encountered. Waiting ${waitMs}ms before retry (Retry-After=${retryAfterHeader})")
                        kotlinx.coroutines.delay(waitMs)
                    } catch (ie: CancellationException) {
                        throw ie
                    }
                    continue
                }

                if (attempt > maxRetries) break
                try {
                    kotlinx.coroutines.delay(delayMs)
                } catch (ie: CancellationException) {
                    throw ie
                }
                delayMs = (delayMs * 2).coerceAtMost(60_000L)
             }
         }
         throw lastEx ?: IllegalStateException("Unknown error in retrying $op")
     }

    // Build text parts for multipart upload
    private fun makeTextParts(
        round: Int,
        modelId: String,
        baseHash: String,
        nSamples: Int,
        payloadKind: String,
        dtype: String,
        inputSize: Int? = null,
        hiddenSize: Int? = null,
        outputSize: Int? = null,
        forceAggregate: Boolean? = null,
        clientMetaJson: String? = null,
        contentSha256: String? = null,
        reqId: String? = null
        , virtualRouterId: String? = null
    ): Map<String, RequestBody> {
        val map = mutableMapOf<String, RequestBody>()
        map["round_id"] = round.toString().toRequestBody(TEXT)
        map["round"] = round.toString().toRequestBody(TEXT)
        map["model_id"] = modelId.toRequestBody(TEXT)
        map["modelId"] = modelId.toRequestBody(TEXT)
        map["base_hash"] = baseHash.toRequestBody(TEXT)
        map["baseHash"] = baseHash.toRequestBody(TEXT)
        map["n_samples"] = nSamples.toString().toRequestBody(TEXT)
        map["nSamples"] = nSamples.toString().toRequestBody(TEXT)
        map["payload_kind"] = payloadKind.toRequestBody(TEXT)
        map["dtype"] = dtype.toRequestBody(TEXT)
        if (dtype == "f32_flat") {
            inputSize?.let { map["input_size"] = it.toString().toRequestBody(TEXT) }
            hiddenSize?.let { map["hidden_size"] = it.toString().toRequestBody(TEXT); map["hiddenSize"] = it.toString().toRequestBody(TEXT) }
            outputSize?.let { map["output_size"] = it.toString().toRequestBody(TEXT); map["outputSize"] = it.toString().toRequestBody(TEXT) }
        }
        forceAggregate?.let { map["force_aggregate"] = it.toString().toRequestBody(TEXT) }
        if (!clientMetaJson.isNullOrBlank()) map["client_meta"] = clientMetaJson.toRequestBody(TEXT)
        // Add alias 'meta' for interoperability: some server implementations expect the key name 'meta' (text part)
        if (!clientMetaJson.isNullOrBlank()) map["meta"] = clientMetaJson.toRequestBody("application/json; charset=utf-8".toMediaType())
        if (!contentSha256.isNullOrBlank()) {
            map["content_sha256"] = contentSha256.toRequestBody(TEXT)
            map["contentSha256"] = contentSha256.toRequestBody(TEXT)
        }
        // run_id: ExperimentContext の実験セッション ID を優先、なければ reqId（リクエスト単位 UUID）を使う
        val effectiveRunId = com.example.hfl_experiment.experiment.ExperimentContext.runId
            .takeIf { it.isNotEmpty() } ?: reqId ?: ""
        if (effectiveRunId.isNotBlank()) {
            map["run_id"] = effectiveRunId.toRequestBody(TEXT)
            map["runId"] = effectiveRunId.toRequestBody(TEXT)
        }
        // virtual router id for logical handover simulation
        if (!virtualRouterId.isNullOrBlank()) {
            map["virtual_router_id"] = virtualRouterId.toRequestBody(TEXT)
            map["virtualRouterId"] = virtualRouterId.toRequestBody(TEXT)
        }
        return map.toMap()
    }

    // Progress-reporting RequestBody for large file uploads
    private class ProgressRequestBody(
        private val file: File,
        private val contentType: okhttp3.MediaType?,
        private val listener: ((sentBytes: Long, totalBytes: Long) -> Unit)?
    ) : RequestBody() {
        override fun contentLength(): Long = file.length()
        override fun contentType(): okhttp3.MediaType? = contentType
        override fun writeTo(sink: okio.BufferedSink) {
            val total = contentLength()
            var sent = 0L
            file.inputStream().use { fis ->
                val buffer = ByteArray(8 * 1024)
                var read = fis.read(buffer)
                while (read >= 0) {
                    sink.write(buffer, 0, read)
                    sent += read
                    try { listener?.invoke(sent, total) } catch (_: Throwable) {}
                    read = fis.read(buffer)
                }
            }
        }
    }

    private fun makeWeightsPartFromFile(file: File, fileName: String, progressCallback: ((Long, Long) -> Unit)? = null): MultipartBody.Part {
        val pr = ProgressRequestBody(file, OCTET, progressCallback)
        return MultipartBody.Part.createFormData("weights", fileName, pr)
    }

    // Wrap request execution to produce extra debug output
    private fun executeRequestWithDebug(client: OkHttpClient, request: Request): okhttp3.Response {
        logRequestDebug(request)
        val start = System.currentTimeMillis()
        val resp = client.newCall(request).execute()
        val end = System.currentTimeMillis()
        // read body as string for preview (but do not consume original stream elsewhere)
        val bodyStr = try { resp.body.string() } catch (e: Exception) { null }
        // Because .body.string() consumes, rebuild a new response with the body re-inserted for callers expecting it is complex.
        // Use this function only where we take full control of response processing.
        RealTimeLogger.i(TAG, "executeRequestWithDebug: executed in ${end - start}ms code=${resp.code}")
        logResponseDebug(resp, bodyStr)
        // Recreate a response with body restored for safe usage (wrap bodyStr back)
        val media = resp.body.contentType()
        val newBody = (bodyStr ?: "").toResponseBody(media)
        return resp.newBuilder().body(newBody).build()
    }

    // Helper to log request and headers before execution (non-sensitive headers only)
    private fun logRequestDebug(request: Request) {
        try {
            val sb = StringBuilder()
            sb.append("HTTP_REQUEST DEBUG: url=${request.url}\n")
            sb.append("method=${request.method}\n")
            val headers = request.headers
            for (i in 0 until headers.size) {
                val name = headers.name(i)
                // hide potentially sensitive values except for User-Agent and content-type
                val value = if (name.equals("Authorization", true)) "[REDACTED]" else headers.value(i)
                sb.append("header: $name: $value\n")
            }
            // try to estimate body size if multipart built (may be -1)
            val body = request.body
            sb.append("hasBody=${body != null} contentLength=${try { body?.contentLength() } catch (_: Exception) { -1 }}\n")
            RealTimeLogger.i(TAG, sb.toString())
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "logRequestDebug failed: ${e.message}")
        }
    }

    // Helper to log response headers/body preview
    private fun logResponseDebug(resp: okhttp3.Response, bodyPreview: String?) {
        try {
            val sb = StringBuilder()
            sb.append("HTTP_RESPONSE DEBUG: code=${resp.code} message=${resp.message} url=${resp.request.url}\n")
            val headers = resp.headers
            for (i in 0 until headers.size) {
                val name = headers.name(i)
                val value = headers.value(i)
                sb.append("respHeader: $name: $value\n")
            }
            if (!bodyPreview.isNullOrBlank()) {
                sb.append("bodyPreview: ${bodyPreview.take(2000)}\n")
            }
            RealTimeLogger.i(TAG, sb.toString())
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "logResponseDebug failed: ${e.message}")
        }
    }

    // Write floats to file and return sha256 hex
    private fun writeFloatArrayToFile(arr: FloatArray, file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        FileOutputStream(file).use { fos ->
            var idx = 0
            val chunk = 4096
            var totalWritten = 0L
            val startAll = System.currentTimeMillis()
            val rt = Runtime.getRuntime()
            RealTimeLogger.i(TAG, "writeFloatArrayToFile: start writing ${arr.size} floats to ${file.absolutePath}")
            while (idx < arr.size) {
                val take = minOf(chunk, arr.size - idx)
                val bb = ByteBuffer.allocate(take * 4).order(ByteOrder.LITTLE_ENDIAN)
                for (i in 0 until take) bb.putFloat(arr[idx + i])
                val bytes = bb.array()
                fos.write(bytes)
                digest.update(bytes)
                idx += take
                totalWritten += bytes.size
                // Log progress every ~1MB or on last chunk
                if (totalWritten % (1 * 1024 * 1024) < bytes.size || idx >= arr.size) {
                    val used = rt.totalMemory() - rt.freeMemory()
                    RealTimeLogger.d(TAG, "writeFloatArrayToFile: progress written=${totalWritten} bytes, idx=${idx}/${arr.size} floats, mem_used=${used} bytes")
                }
            }
            val endAll = System.currentTimeMillis()
            RealTimeLogger.i(TAG, "writeFloatArrayToFile: finished writing ${file.length()} bytes in ${endAll - startAll}ms")
        }
        val hash = digest.digest()
        return hash.joinToString("") { "%02x".format(it) }
    }

    // Check server marker endpoint (best-effort). Returns false on error.
    private fun checkServerMarker(sha: String): Boolean {
        return try {
            val url = ensureEndsWithSlash(baseUrl) + "api/markers/hash/$sha"
            val req = Request.Builder().url(url).get().build()
            client.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return false
                val body = resp.body.string()
                val jo = JSONObject(body)
                jo.optBoolean("exists", false)
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "checkServerMarker failed: ${e.message}")
            false
        }
    }

    // Mark as sent in shared prefs and try DB
    private fun markAsSent(sha: String, reqId: String?, roundId: Int?, ackTimestamp: String? = null) {
        try {
            sharedPreferences.edit().putBoolean("sent_${sha}", true).apply()
        } catch (_: Throwable) { }
        try {
            dbHelper.markAck(sha, reqId, roundId, ackTimestamp)
        } catch (_: Throwable) { }
    }

    // New debug helper: dump upload_logs.csv to logcat so we can retrieve it via `adb logcat -d -s UPLOAD_CSV_DUMP`
    fun dumpUploadLogsToLogcat() {
        try {
            val logFile = File(context.filesDir, "upload_logs.csv")
            if (!logFile.exists()) {
                RealTimeLogger.i(TAG, "UPLOAD_CSV_DUMP: upload_logs.csv not found at ${logFile.absolutePath}")
                return
            }
            logFile.bufferedReader().useLines { lines ->
                RealTimeLogger.i(TAG, "UPLOAD_CSV_DUMP: start dump of upload_logs.csv")
                var i = 0
                for (line in lines) {
                    // prefix each line so it's easy to grep
                    RealTimeLogger.i("UPLOAD_CSV_DUMP", line)
                    i++
                    // avoid flooding logcat too much; but still output all lines
                }
                RealTimeLogger.i(TAG, "UPLOAD_CSV_DUMP: end dump, lines=$i")
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "dumpUploadLogsToLogcat failed: ${e.message}")
        }
    }

    fun makeMultipartRequest(url: String, parts: Map<String, RequestBody>, weightsPart: MultipartBody.Part): Request {
        val multipartBuilder = MultipartBody.Builder().setType(MultipartBody.FORM)
        // Add text parts using RequestBody to ensure content-type headers are preserved and the server can parse them reliably.
        parts.forEach { (k, v) ->
            try {
                multipartBuilder.addFormDataPart(k, null, v)
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "makeMultipartRequest: failed to add part $k via RequestBody, falling back to string: ${e.message}")
                try {
                    val buffer = okio.Buffer()
                    v.writeTo(buffer)
                    multipartBuilder.addFormDataPart(k, buffer.readUtf8())
                } catch (_: Throwable) { /* best-effort */ }
            }
        }
        multipartBuilder.addPart(weightsPart)
        val req = Request.Builder()
            .url(ensureEndsWithSlash(baseUrl) + url.trimStart('/'))
            .post(multipartBuilder.build())
            .build()
        try {
            RealTimeLogger.d(TAG, "makeMultipartRequest: url=${req.url} parts=${parts.keys}")
        } catch (e: Exception) { }
        return req
    }

    // --- New thin helpers using Retrofit interfaces for the simpler TrainingDataApi/ClientLogsApi usage ---
    // These helpers are convenience wrappers; they don't replace the existing robust upload methods but provide a small API for simple metric/file uploads.
    suspend fun sendMetricsViaRetrofit(baseUrlOverride: String? = null, metrics: com.example.hfl_experiment.network.api.TrainingMetrics): Boolean = withContext(Dispatchers.IO) {
        try {
            val retrofit = Retrofit.Builder()
                .baseUrl(ensureEndsWithSlash(baseUrlOverride ?: baseUrl))
                .client(defaultOkHttpClient())
                .addConverterFactory(GsonConverterFactory.create())
                .build()
            val api = retrofit.create(com.example.hfl_experiment.network.api.TrainingDataApi::class.java)
            val resp = api.sendMetrics(metrics)
            if (!resp.isSuccessful) {
                RealTimeLogger.w(TAG, "sendMetricsViaRetrofit: server returned http=${resp.code()} body=${resp.errorBody()?.string()}")
                return@withContext false
            }
            RealTimeLogger.i(TAG, "sendMetricsViaRetrofit: success code=${resp.code()} body=${resp.body()}")
            return@withContext true
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "sendMetricsViaRetrofit failed: ${e.message}")
            return@withContext false
        }
    }

    suspend fun uploadClientLogsViaRetrofit(baseUrlOverride: String? = null, terminalId: String, files: Map<String, java.io.File>, metaJson: String?, authToken: String): Boolean = withContext(Dispatchers.IO) {
        try {
            val clientShort = defaultOkHttpClient()
            val retrofit = Retrofit.Builder()
                .baseUrl(ensureEndsWithSlash(baseUrlOverride ?: baseUrl))
                .client(clientShort)
                .addConverterFactory(GsonConverterFactory.create())
                .build()
            val api = retrofit.create(com.example.hfl_experiment.network.api.ClientLogsApi::class.java)

            val parts = files.map { (k, f) ->
                // ★修正箇所: toMediaTypeOrNull() -> toMediaType()
                val rb = f.asRequestBody("application/octet-stream".toMediaType())
                MultipartBody.Part.createFormData(k, f.name, rb)
            }
            // ★修正箇所: toMediaTypeOrNull() -> toMediaType()
            val metaBody = metaJson?.toRequestBody("application/json".toMediaType())
            val auth = "Bearer $authToken"
            val resp = api.uploadClientLogs(terminalId, metaBody, parts, auth)
            if (!resp.isSuccessful) {
                RealTimeLogger.w(TAG, "uploadClientLogsViaRetrofit: server returned http=${resp.code()} body=${resp.errorBody()?.string()}")
                return@withContext false
            }
            RealTimeLogger.i(TAG, "uploadClientLogsViaRetrofit: success body=${resp.body()}")
            return@withContext true
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "uploadClientLogsViaRetrofit failed: ${e.message}")
            return@withContext false
        }
    }


    // Send device telemetry JSON to server endpoint /api/v1/telemetry/{terminalId}
    suspend fun sendTelemetry(terminalId: String, telemetryJson: String): Boolean = withContext(Dispatchers.IO) {
        try {
            val url = ensureEndsWithSlash(baseUrl) + "api/v1/telemetry/$terminalId"
            val body = telemetryJson.toRequestBody("application/json; charset=utf-8".toMediaType())
            val builder = Request.Builder()
                .url(url)
                .post(body)
            // Prefer token provided to this NetworkClient (authToken property) if set, otherwise AppConfig fallback
            val actualToken = authToken ?: try { AppConfig.getServerAuthToken(context) } catch (_: Exception) { null }
             if (!actualToken.isNullOrBlank()) {
                 builder.header("Authorization", "Bearer $actualToken")
             }
             val req = builder.build()

            val start = System.currentTimeMillis()
            client.newCall(req).execute().use { resp ->
                val dur = System.currentTimeMillis() - start
                val bodyStr = try { resp.body?.string() } catch (_: Exception) { null }
                if (!resp.isSuccessful) {
                    // Detailed log: include HTTP code, headers, and response body preview to help diagnose 401/403 or connectivity issues
                    val hdrs = try { resp.headers.toString() } catch (_: Exception) { "" }
                    RealTimeLogger.w(TAG, "sendTelemetry failed: code=${resp.code} duration=${dur}ms headers=${hdrs} body=${bodyStr}")
                    return@withContext false
                }
                RealTimeLogger.i(TAG, "sendTelemetry: success code=${resp.code} duration=${dur}ms bodyPreview=${bodyStr?.take(200)}")
                return@withContext true
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "sendTelemetry exception: ${e.message}")
            return@withContext false
        }
    }

    // Helper to detect server responses that indicate the upload was already accepted/duplicated.
    // Returns true if the response (code + body) should be treated as ACK/success rather than an error.
    private fun isDuplicateOrWeightsMismatch(respCode: Int, bodyStr: String?): Boolean {
        if (bodyStr.isNullOrBlank()) return false
        val lc = bodyStr.lowercase()
        if (respCode == 400 && lc.contains("weights size mismatch")) return true
        if (lc.contains("duplicate") || lc.contains("duplicate_ignored")) return true
        return false
    }

    /**
     * Checks if a new model has been applied on the server.
     * This is a placeholder implementation and should be replaced with actual logic.
     */
    suspend fun checkNewModelApplied(): Boolean = withContext(Dispatchers.IO) {
        try {
            // val response = api.checkModelStatus() // Assuming `checkModelStatus` exists in `ApiService`
            // return@withContext response.isSuccessful && response.body()?.isNewModelApplied == true
            return@withContext false
        } catch (e: Exception) {
            RealTimeLogger.e(TAG, "Error checking new model status", e)
            return@withContext false
        }
    }
}
