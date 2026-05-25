package com.example.hfl_experiment.device

import android.content.Context
import com.example.hfl_experiment.training.data.TrainingLogger
import com.example.hfl_experiment.util.logging.RealTimeLogger
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import okio.buffer
import okio.sink
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

/**
 * DeviceAckClient
 * - WebSocket でエッジの /ws/updates を購読し、model_update を受けたらモデルをダウンロード・検証・適用し、ACK を POST するクライアント
 * - シンプルなコールバック方式でモデル適用（load/apply）処理を呼び出します。
 *
 * Usage:
 * val client = DeviceAckClient(context, edgeHost = "edge.local:8001", token = token, deviceId = "device-01")
 * client.start { modelFile ->
 *     // apply the model (load into trainer); return true on success
 *     myTrainer.loadFromFile(modelFile)
 *     true
 * }
 */
class DeviceAckClient(
    private val context: Context,
    private val edgeHost: String,
    private val token: String? = null,
    private val deviceId: String,
    private val trainingLogger: TrainingLogger? = null
) {
    private val TAG = "DeviceAckClient"
    private val client = OkHttpClient.Builder()
        .callTimeout(0, TimeUnit.SECONDS) // 0 = タイムアウト無効（WebSocket長期接続のため）
        .addInterceptor { chain ->
            // 実験追跡ヘッダを WebSocket ハンドシェイク含む全リクエストに付与
            val req = chain.request().newBuilder()
                .addHeader("X-Run-Id", com.example.hfl_experiment.experiment.ExperimentContext.runId.ifEmpty { "none" })
                .addHeader("X-Terminal-Id", com.example.hfl_experiment.experiment.ExperimentContext.terminalId.ifEmpty { "unknown" })
                .build()
            chain.proceed(req)
        }
        .build()

    private var webSocket: WebSocket? = null
    private val scheduler: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor()
    private var reconnectAttempts = 0

    // Backoff settings
    private val initialBackoffMs = 1000L
    private val maxBackoffMs = 60_000L

    private var applyCallback: ((File) -> Boolean)? = null

    // normalize host (remove any scheme) and determine default http scheme to use for download/ack
    private val baseHost: String = edgeHost
        .removePrefix("http://")
        .removePrefix("https://")
        .removePrefix("wss://")
        .removePrefix("ws://")
        .trimEnd('/')

    // default to https unless the provided edgeHost explicitly started with http:// or ws://
    private val defaultHttpScheme: String = when {
        edgeHost.startsWith("http://", ignoreCase = true) || edgeHost.startsWith("ws://", ignoreCase = true) -> "http"
        else -> "https"
    }

    fun start(applyCallback: (File) -> Boolean) {
        this.applyCallback = applyCallback
        connectWebSocket()
    }

    fun stop() {
        webSocket?.close(1000, "client_stop")
        webSocket = null
        scheduler.shutdownNow()
        try { scheduler.awaitTermination(5, TimeUnit.SECONDS) } catch (_: Exception) {}
        client.dispatcher.executorService.shutdown()
        client.connectionPool.evictAll()
    }

    private fun connectWebSocket() {
        val scheme = when {
            edgeHost.startsWith("wss://", ignoreCase = true) -> "wss"
            edgeHost.startsWith("ws://", ignoreCase = true) -> "ws"
            edgeHost.startsWith("https://", ignoreCase = true) -> "wss"
            edgeHost.startsWith("http://", ignoreCase = true) -> "ws"
            else -> "wss" // default to secure
        }
        val wsUrl = "$scheme://$baseHost/ws/updates" + (if (!token.isNullOrBlank()) "?token=$token" else "")
        val req = Request.Builder().url(wsUrl).build()
        logLocal("websocket_connect_attempt", -1, wsUrl)
        webSocket = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                RealTimeLogger.i(TAG, "WebSocket opened: $wsUrl")
                reconnectAttempts = 0
                logLocal("websocket_open", -1, wsUrl)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    handleMessage(text)
                } catch (e: Exception) {
                    RealTimeLogger.e(TAG, "onMessage error: ${e.message}", e)
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                val hint = if (t is java.net.ConnectException ||
                    t.message?.contains("ECONNREFUSED", ignoreCase = true) == true
                ) {
                    " [ヒント: エッジが起動しているか、端末からホスト:ポートに届くか(同一LAN・FW)を確認]"
                } else ""
                RealTimeLogger.e(TAG, "WebSocket failure: ${t.message}$hint")
                logLocal("websocket_failure", -1, (t.message ?: "") + hint)
                scheduleReconnect()
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                RealTimeLogger.i(TAG, "WebSocket closed: $code / $reason")
                logLocal("websocket_closed", -1, "$code:$reason")
                scheduleReconnect()
            }
        })

        // Validate wsUrl before using it
        if (wsUrl.isBlank()) {
            RealTimeLogger.e(TAG, "WebSocket URL is invalid: $wsUrl")
            return
        }
    }

    private fun scheduleReconnect() {
        reconnectAttempts++
        val backoff = (initialBackoffMs * (1 shl (reconnectAttempts.coerceAtMost(10))))
            .coerceAtMost(maxBackoffMs).toLong() // Int → Long の型変換を削除
        // jitter
        val jitter = (Math.random() * 500).toLong()
        val delay = backoff + jitter
        RealTimeLogger.i(TAG, "Scheduling reconnect in ${delay}ms (attempt=${reconnectAttempts})")
        scheduler.schedule({ connectWebSocket() }, delay, TimeUnit.MILLISECONDS)
    }

    private fun handleMessage(text: String) {
        val obj = JSONObject(text)
        val type = obj.optString("type")
        if (type != "model_update") return
        val payload = obj.getJSONObject("payload")
        val notificationId = payload.getString("notification_id")
        val modelId = payload.optString("model_id", null) ?: "" // デフォルト値を空文字列に設定
        val round = if (payload.has("round")) payload.optInt("round") else null
        val downloadUrl = if (payload.has("download_url")) payload.getString("download_url") else null
        val downloadRel = if (payload.has("download_rel")) payload.getString("download_rel") else null
        val sha256 = payload.optString("sha256", null) ?: "" // デフォルト値を空文字列に設定

        logLocal("notification_received", round ?: -1, notificationId)

        // perform download + apply in background
        scheduler.execute {
            var tmpFile: File? = null
            var applied = false
            var applyStartMs = 0L
            try {
                val url = when {
                    !downloadUrl.isNullOrBlank() -> downloadUrl
                    !downloadRel.isNullOrBlank() -> "$defaultHttpScheme://$baseHost/download?rel_path=$downloadRel"
                    else -> throw IllegalArgumentException("no download url in payload")
                }

                logLocal("download_start", round ?: -1, url)
                tmpFile = downloadToTemp(url)
                logLocal("download_complete", round ?: -1, tmpFile.absolutePath)

                if (!sha256.isNullOrBlank()) {
                    val ok = verifySha256(tmpFile, sha256)
                    logLocal("sha256_check", round ?: -1, if (ok) "ok" else "mismatch")
                    if (!ok) throw IOException("sha256 mismatch")
                }

                // move to final file
                val finalFile = File(context.filesDir, "model.pt")
                tmpFile.renameTo(finalFile) // 冗長な let を削除

                // apply
                applyStartMs = System.currentTimeMillis()
                val applyCb = applyCallback
                val applyOk = applyCb?.invoke(finalFile) ?: false
                val applyEndMs = System.currentTimeMillis()
                val durationMs = (applyEndMs - applyStartMs).toInt()
                logLocal("apply_complete", round ?: -1, "duration_ms=$durationMs success=$applyOk")

                if (applyOk) {
                    applied = true
                    sendAck(notificationId, modelId, round, applyStartMs, durationMs)
                } else {
                    sendError(notificationId, modelId, round, "apply_failed")
                }
            } catch (e: Exception) {
                RealTimeLogger.e(TAG, "apply error: ${e.message}", e)
                logLocal("apply_error", round ?: -1, e.message ?: "")
                try {
                    sendError(notificationId, modelId, round, e.message ?: "error")
                } catch (ignored: Exception) { // ignored を削除
                    // ログ出力などを追加する場合はここに記述
                }
            } finally {
                // cleanup tmp if exists
                // (if rename succeeded tmpFile will be null or not exist)
            }
        }
    }

    @Throws(IOException::class)
    private fun downloadToTemp(url: String): File {
        val req = Request.Builder().url(url).get().build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw IOException("download failed code=${resp.code}")
            val tmp = File.createTempFile("model", ".pt", context.cacheDir)
            val sink: BufferedSink = tmp.sink().buffer()
            try {
                resp.body?.source()?.let { sink.writeAll(it) }
            } finally {
                sink.close()
            }
            return tmp
        }
    }

    private fun verifySha256(file: File?, expectedHex: String): Boolean {
        if (file == null) return false
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { fis ->
            val buf = ByteArray(8192)
            var read: Int
            while (fis.read(buf).also { read = it } > 0) {
                md.update(buf, 0, read)
            }
        }
        val actual = md.digest().joinToString("") { "%02x".format(it) }
        return actual.equals(expectedHex, ignoreCase = true)
    }

    private fun sendAck(notificationId: String, modelId: String?, round: Int?, applyStartMs: Long, applyDurationMs: Int) {
        val sdf = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US)
        val eventTs = sdf.format(Date(applyStartMs))
        val body = JSONObject()
        body.put("device_id", deviceId)
        body.put("notification_id", notificationId)
        if (modelId != null) body.put("model_id", modelId)
        if (round != null) body.put("round", round)
        body.put("event_timestamp", eventTs)
        body.put("apply_duration_ms", applyDurationMs)

        val url = "$defaultHttpScheme://$baseHost/device_ack"
        val reqBody = body.toString().toRequestBody("application/json".toMediaType()) // 非推奨 API を最新の拡張関数に置き換え
        val req = Request.Builder().url(url).post(reqBody).build()
        try {
            client.newCall(req).execute().use { resp ->
                val txt = resp.body?.string()
                RealTimeLogger.i(TAG, "ACK sent resp=${resp.code} body=$txt")
                logLocal("ack_sent", round ?: -1, "code=${resp.code} body=$txt")
            }
        } catch (e: Exception) {
            RealTimeLogger.e(TAG, "ack send failed: ${e.message}", e)
            logLocal("ack_failed", round ?: -1, e.message ?: "")
        }
    }

    private fun sendError(notificationId: String, modelId: String?, round: Int?, errorMsg: String) {
        val body = JSONObject()
        body.put("device_id", deviceId)
        body.put("notification_id", notificationId)
        if (modelId != null) body.put("model_id", modelId)
        if (round != null) body.put("round", round)
        body.put("error", errorMsg)

        val url = "$defaultHttpScheme://$baseHost/device_error"
        val reqBody = body.toString().toRequestBody("application/json".toMediaType()) // 非推奨 API を最新の拡張関数に置き換え
        val req = Request.Builder().url(url).post(reqBody).build()
        try {
            client.newCall(req).execute().use { resp ->
                val txt = resp.body?.string()
                RealTimeLogger.i(TAG, "Error posted resp=${resp.code} body=$txt")
                logLocal("error_posted", round ?: -1, "code=${resp.code} body=$txt")
            }
        } catch (e: Exception) {
            RealTimeLogger.e(TAG, "error post failed: ${e.message}", e)
            logLocal("error_post_failed", round ?: -1, e.message ?: "")
        }
    }

    private fun logLocal(event: String, round: Int, extra: String) {
        try {
            trainingLogger?.logEvent(event, round, extra)
        } catch (_: Exception) {
            // ignore
        }
    }
}
