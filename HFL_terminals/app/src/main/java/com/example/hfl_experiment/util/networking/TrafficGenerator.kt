package com.example.hfl_experiment.util.networking

import android.content.Context
import com.example.hfl_experiment.AppConfig
import kotlinx.coroutines.*
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.RequestBody.Companion.toRequestBody
import okio.Buffer
import com.example.hfl_experiment.util.logging.RealTimeLogger
import kotlin.math.ln
import kotlin.random.Random
import java.util.concurrent.TimeUnit

/**
 * Simple traffic generator utilities.
 * - startDownloadLoop(url) repeatedly fetches the given URL and discards the body to generate downlink traffic.
 *   It returns a Job which can be cancelled to stop the load.
 */
object TrafficGenerator {
    private const val TAG = "TrafficGenerator"

    // simple metric carrier for callbacks
    data class Metric(val name: String, val value: Double, val tags: Map<String, String> = emptyMap())

    // --- Context-based overloads: resolve URL via AppConfig helpers and delegate ---
    fun startDownloadLoop(context: Context, client: OkHttpClient = OkHttpClient(), maxBytesPerFetch: Long = 2L * 1024 * 1024): Job {
        val url = AppConfig.getDownloadBaseUrl(context)
        return startDownloadLoop(url, client, maxBytesPerFetch)
    }

    fun startUploadLoop(context: Context, client: OkHttpClient = OkHttpClient(), payloadSize: Int = 64 * 1024, intervalMs: Long = 200, onMetric: (Metric) -> Unit = {}): Job {
        val url = AppConfig.getEdgeBaseUrl(context).trimEnd('/') + "/receive_terminal_weights/${AppConfig.getTerminalId(context)}"
        return startUploadLoop(url, client, payloadSize, intervalMs, onMetric)
    }

    fun startPingLoop(context: Context, client: OkHttpClient = OkHttpClient(), intervalMs: Long = 1000, onMetric: (Metric) -> Unit = {}): Job {
        val url = AppConfig.getEdgeBaseUrl(context).trimEnd('/') + "/api/v1/meta"
        return startPingLoop(url, client, intervalMs, onMetric)
    }

    fun startPoissonTrafficLoop(context: Context, client: OkHttpClient = OkHttpClient(), lambdaPerSec: Double = 0.5, fetchSizeBytes: Long = 256 * 1024, onMetric: (Metric) -> Unit = {}): Job {
        val url = AppConfig.getEdgeBaseUrl(context).trimEnd('/') + "/resource"
        return startPoissonTrafficLoop(url, client, lambdaPerSec, fetchSizeBytes, onMetric)
    }

    // --- URL-based implementations (existing) ---
    fun startDownloadLoop(url: String, client: OkHttpClient = OkHttpClient(), maxBytesPerFetch: Long = 2L * 1024 * 1024): Job {
        val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
        return scope.launch {
            RealTimeLogger.i(TAG, "startDownloadLoop: starting loop for url=$url")
            try {
                while (isActive) {
                    try {
                        val req = Request.Builder().url(url).get().build()
                        client.newCall(req).execute().use { resp ->
                            if (!resp.isSuccessful) {
                                RealTimeLogger.w(TAG, "startDownloadLoop: server returned ${resp.code} for $url")
                            } else {
                                val body = resp.body
                                if (body != null) {
                                    val source = body.source()
                                    val buf = Buffer()
                                    var readTotal = 0L
                                    while (isActive && readTotal < maxBytesPerFetch) {
                                        val r = source.read(buf, 8 * 1024)
                                        if (r <= 0) break
                                        readTotal += r
                                    }
                                    // discard buffer
                                    buf.clear()
                                    RealTimeLogger.d(TAG, "startDownloadLoop: fetched bytes=$readTotal url=$url")
                                }
                            }
                        }
                    } catch (e: Exception) {
                        RealTimeLogger.w(TAG, "startDownloadLoop: fetch failed: ${e.message}")
                    }
                    // short pause to avoid tight loop; adjust as needed
                    delay(200)
                }
            } catch (e: CancellationException) {
                RealTimeLogger.i(TAG, "startDownloadLoop: cancelled")
                throw e
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "startDownloadLoop: loop failed: ${e.message}")
            } finally {
                RealTimeLogger.i(TAG, "startDownloadLoop: exiting loop for url=$url")
            }
        }
    }

    /**
     * 継続的に POST を投げてアップロードトラフィックを生成するループ
     * payloadSize: 1 リクエストあたりのバイト数
     * intervalMs: リクエスト間隔
     */
    fun startUploadLoop(
        url: String,
        client: OkHttpClient = OkHttpClient(),
        payloadSize: Int = 64 * 1024,
        intervalMs: Long = 200,
        onMetric: (Metric) -> Unit = {}
    ): Job {
        val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
        val mediaType = "application/octet-stream".toMediaTypeOrNull()
        val payload = ByteArray(payloadSize) { 0 }
        return scope.launch {
            RealTimeLogger.i(TAG, "startUploadLoop: starting for $url payload=$payloadSize")
            try {
                while (isActive) {
                    val req = Request.Builder()
                        .url(url)
                        .post(payload.toRequestBody(mediaType))
                        .build()
                    val start = System.nanoTime()
                    try {
                        client.newCall(req).execute().use { resp ->
                            val elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start)
                            onMetric(Metric("upload_rtt_ms", elapsedMs.toDouble(), mapOf("url" to url)))
                            onMetric(Metric("upload_bytes", payloadSize.toDouble(), mapOf("url" to url)))
                            if (!resp.isSuccessful) {
                                RealTimeLogger.w(TAG, "startUploadLoop: server returned ${resp.code} for $url")
                            }
                        }
                    } catch (e: Exception) {
                        RealTimeLogger.w(TAG, "startUploadLoop: upload failed: ${e.message}")
                    }
                    delay(intervalMs)
                }
            } catch (e: CancellationException) {
                RealTimeLogger.i(TAG, "startUploadLoop: cancelled")
                throw e
            } finally {
                RealTimeLogger.i(TAG, "startUploadLoop: exiting for $url")
            }
        }
    }

    /**
     * 短い HEAD リクエストで RTT を計測するループ
     */
    fun startPingLoop(
        url: String,
        client: OkHttpClient = OkHttpClient(),
        intervalMs: Long = 1000,
        onMetric: (Metric) -> Unit = {}
    ): Job {
        val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
        return scope.launch {
            RealTimeLogger.i(TAG, "startPingLoop: starting ping for $url")
            try {
                while (isActive) {
                    val req = Request.Builder().url(url).head().build()
                    val start = System.nanoTime()
                    try {
                        client.newCall(req).execute().use { resp ->
                            val elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start)
                            onMetric(Metric("rtt_ms", elapsedMs.toDouble(), mapOf("url" to url, "code" to resp.code.toString())))
                        }
                    } catch (e: Exception) {
                        RealTimeLogger.w(TAG, "startPingLoop: ping failed: ${e.message}")
                        onMetric(Metric("rtt_ms_failed", 1.0, mapOf("url" to url)))
                    }
                    delay(intervalMs)
                }
            } catch (e: CancellationException) {
                RealTimeLogger.i(TAG, "startPingLoop: cancelled")
                throw e
            } finally {
                RealTimeLogger.i(TAG, "startPingLoop: exiting ping for $url")
            }
        }
    }

    /**
     * ポアソン（指数分布）間隔でダウンロード要求を発生させるループ
     * lambdaPerSec: 平均到着率 (1/sec)
     */
    fun startPoissonTrafficLoop(
        url: String,
        client: OkHttpClient = OkHttpClient(),
        lambdaPerSec: Double = 0.5,
        fetchSizeBytes: Long = 256 * 1024,
        onMetric: (Metric) -> Unit = {}
    ): Job {
        val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
        return scope.launch {
            RealTimeLogger.i(TAG, "startPoissonTrafficLoop: starting for $url lambda=$lambdaPerSec")
            try {
                while (isActive) {
                    // 指数分布で次到着間隔をサンプル
                    val u = Random.nextDouble().coerceAtLeast(1e-12)
                    val intervalSec = -ln(u) / lambdaPerSec
                    val intervalMs = (intervalSec * 1000).toLong()
                    delay(intervalMs)

                    try {
                        val req = Request.Builder().url(url).get().build()
                        client.newCall(req).execute().use { resp ->
                            if (resp.isSuccessful) {
                                val body = resp.body
                                if (body != null) {
                                    val source = body.source()
                                    val buf = Buffer()
                                    var readTotal = 0L
                                    while (isActive && readTotal < fetchSizeBytes) {
                                        val r = source.read(buf, 8 * 1024)
                                        if (r <= 0) break
                                        readTotal += r
                                    }
                                    buf.clear()
                                    onMetric(Metric("poisson_bytes", readTotal.toDouble(), mapOf("url" to url)))
                                }
                            } else {
                                RealTimeLogger.w(TAG, "startPoissonTrafficLoop: server ${resp.code}")
                            }
                        }
                    } catch (e: Exception) {
                        RealTimeLogger.w(TAG, "startPoissonTrafficLoop: fetch failed: ${e.message}")
                    }
                }
            } catch (e: CancellationException) {
                RealTimeLogger.i(TAG, "startPoissonTrafficLoop: cancelled")
                throw e
            } finally {
                RealTimeLogger.i(TAG, "startPoissonTrafficLoop: exiting for $url")
            }
        }
    }
}
