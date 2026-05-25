package com.example.hfl_experiment.telemetry

import android.app.ActivityManager
import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream

/**
 * 実機精度低下分析のための軽量メトリクス収集器。
 *
 * Perfettoでは得にくい/リアルタイム性が必要な変数をアプリ内で直接採取する：
 * - memory_low_flag_count: メモリ逼迫検知回数
 * - dropout_epoch_count: 途中打ち切りされたエポック数
 * - tcp_handshake_latency_ms: HTTP接続確立までの時間
 * - handover_offline_ms: AP切替時のオフライン時間
 * - network_idle_wait_ms: 通信完了→計算開始の空白時間
 *
 * 学習セッションごとにリセットし、終了時に JSONL ファイルへ追記する。
 */
object DeviceMetricsCollector {
    private const val FILE_NAME = "device_metrics.jsonl"
    private var baseDir: File? = null

    // ── メモリ逼迫 ──────────────────────────────────────────────────────
    @Volatile var memoryLowFlagCount: Int = 0
        private set

    // ── エポックドロップアウト ────────────────────────────────────────────
    /** 予定エポック数 */
    var expectedEpochCount: Int = 0
    /** 実際に完了したエポック数 */
    var completedEpochCount: Int = 0
    /** ドロップアウト: expected - completed */
    val dropoutEpochCount: Int get() = (expectedEpochCount - completedEpochCount).coerceAtLeast(0)

    // ── TCP ハンドシェイク ────────────────────────────────────────────────
    private val handshakeLatencies = mutableListOf<Long>()
    val tcpHandshakeLatencyMs: Long?
        get() = if (handshakeLatencies.isNotEmpty()) handshakeLatencies.average().toLong() else null

    // ── ハンドオーバオフライン ────────────────────────────────────────────
    @Volatile private var lastConnectedTs: Long = 0L
    @Volatile private var lastDisconnectedTs: Long = 0L
    private val handoverOfflineDurations = mutableListOf<Long>()
    val handoverOfflineMs: Long?
        get() = if (handoverOfflineDurations.isNotEmpty()) handoverOfflineDurations.sum() else null

    // ── ネットワーク空白時間（通信完了→計算開始） ─────────────────────────
    @Volatile var lastNetworkCompleteTs: Long = 0L
    @Volatile var lastComputeStartTs: Long = 0L
    val networkIdleWaitMs: Long?
        get() {
            if (lastNetworkCompleteTs <= 0 || lastComputeStartTs <= 0) return null
            val diff = lastComputeStartTs - lastNetworkCompleteTs
            return if (diff > 0) diff else null
        }

    // ── スレッド待機時間（シリアライズ/非同期完了待ち） ───────────────────
    private val threadWaitDurations = mutableListOf<Long>()
    val threadWaitMs: Long?
        get() = if (threadWaitDurations.isNotEmpty()) threadWaitDurations.sum() else null

    // ── ペイロード vs 通信全体時間（HTTPオーバーヘッド計測） ──────────────
    @Volatile var lastPayloadTransferMs: Long = 0L
    @Volatile var lastTotalUploadMs: Long = 0L
    val payloadVsTotalRatio: Double?
        get() {
            if (lastTotalUploadMs <= 0 || lastPayloadTransferMs <= 0) return null
            return lastPayloadTransferMs.toDouble() / lastTotalUploadMs.toDouble()
        }

    // ── 初期化 / リセット ────────────────────────────────────────────────

    fun init(context: Context) {
        baseDir = context.filesDir
    }

    fun resetForSession(expectedEpochs: Int) {
        memoryLowFlagCount = 0
        expectedEpochCount = expectedEpochs
        completedEpochCount = 0
        handshakeLatencies.clear()
        handoverOfflineDurations.clear()
        lastNetworkCompleteTs = 0L
        lastComputeStartTs = 0L
        lastConnectedTs = System.currentTimeMillis()
        lastDisconnectedTs = 0L
        threadWaitDurations.clear()
        lastPayloadTransferMs = 0L
        lastTotalUploadMs = 0L
    }

    // ── 記録メソッド ─────────────────────────────────────────────────────

    fun checkMemoryPressure(context: Context) {
        try {
            val am = context.getSystemService(Context.ACTIVITY_SERVICE) as? ActivityManager ?: return
            val mi = ActivityManager.MemoryInfo()
            am.getMemoryInfo(mi)
            if (mi.lowMemory) {
                memoryLowFlagCount++
            }
        } catch (_: Exception) {}
    }

    fun recordEpochCompleted() {
        completedEpochCount++
    }

    fun recordHandshakeLatency(ms: Long) {
        if (ms > 0) handshakeLatencies.add(ms)
    }

    fun onNetworkDisconnected() {
        lastDisconnectedTs = System.currentTimeMillis()
    }

    fun onNetworkReconnected() {
        val now = System.currentTimeMillis()
        lastConnectedTs = now
        if (lastDisconnectedTs > 0) {
            val offlineMs = now - lastDisconnectedTs
            if (offlineMs > 0 && offlineMs < 60_000) { // sanity: max 60s
                handoverOfflineDurations.add(offlineMs)
            }
            lastDisconnectedTs = 0L
        }
    }

    fun markNetworkComplete() {
        lastNetworkCompleteTs = System.currentTimeMillis()
    }

    fun markComputeStart() {
        lastComputeStartTs = System.currentTimeMillis()
    }

    fun recordThreadWait(durationMs: Long) {
        if (durationMs > 0) threadWaitDurations.add(durationMs)
    }

    fun recordUploadTiming(payloadMs: Long, totalMs: Long) {
        if (payloadMs > 0) lastPayloadTransferMs = payloadMs
        if (totalMs > 0) lastTotalUploadMs = totalMs
    }

    // ── スナップショット取得 ─────────────────────────────────────────────

    fun snapshot(): JSONObject {
        return JSONObject().apply {
            put("timestamp_ms", System.currentTimeMillis())
            put("memory_low_flag_count", memoryLowFlagCount)
            put("dropout_epoch_count", dropoutEpochCount)
            put("expected_epoch_count", expectedEpochCount)
            put("completed_epoch_count", completedEpochCount)
            put("tcp_handshake_latency_ms", tcpHandshakeLatencyMs ?: JSONObject.NULL)
            put("handover_offline_ms", handoverOfflineMs ?: JSONObject.NULL)
            put("network_idle_wait_ms", networkIdleWaitMs ?: JSONObject.NULL)
            put("thread_wait_ms", threadWaitMs ?: JSONObject.NULL)
            put("payload_transfer_ms", lastPayloadTransferMs.takeIf { it > 0 } ?: JSONObject.NULL)
            put("total_upload_ms", lastTotalUploadMs.takeIf { it > 0 } ?: JSONObject.NULL)
            put("payload_vs_total_ratio", payloadVsTotalRatio ?: JSONObject.NULL)
        }
    }

    // ── 永続化 ───────────────────────────────────────────────────────────

    fun persist(sessionId: String, round: Int) {
        try {
            val dir = baseDir ?: return
            val jo = snapshot()
            jo.put("session_id", sessionId)
            jo.put("round", round)
            val line = jo.toString() + "\n"
            synchronized(this) {
                FileOutputStream(File(dir, FILE_NAME), true).use {
                    it.write(line.toByteArray())
                }
            }
        } catch (_: Exception) {}
    }
}
