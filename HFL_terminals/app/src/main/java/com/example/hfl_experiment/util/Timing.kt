package com.example.hfl_experiment.util

import android.util.Log
import com.example.hfl_experiment.util.logging.RealTimeLogger

object Timing {
    // このユーティリティ用のログタグ
    private const val TAG = "Timing"

    // --- 調整ポイント (tunable) ---
    // Maximum backoff cap used in NetworkClient.retrying (previously 30000L)
    // Lower this to reduce long wait when retrying (default 10000ms)
    // Reduced from 5000ms -> 2000ms to make retries faster on mobile devices
    // TIP: Tweak this when you want shorter/longer max exponential backoff for network retries.
    // 上限値: ネットワークリトライの指数バックオフの上限を指定します（以前は 30000ms）。
    // 小さくするとリトライ時の長時間待機を減らせます。モバイルでは短めにしておくと反応が早くなります。
    const val MAX_RETRY_DELAY_MS: Long = 2_000L

    // Upload completion polling: attempts * delay_ms = total poll window
    // Previously: attempts=12, delayMs=2500L -> 30_000ms
    // TIP: Increase UPLOAD_POLL_ATTEMPTS to check status more frequently (smaller delay),
    // or increase UPLOAD_POLL_DELAY_MS to reduce request rate.
    // アップロード完了確認のポーリング設定: attempts×delay_ms が総監視時間になります。
    // TIP: attempts を増やすとより頻繁にチェック（delay が小さくなる）されます。delay を増やすとリクエスト頻度を下げられます。
    @Suppress("unused")
    const val UPLOAD_POLL_ATTEMPTS: Int = 5
    @Suppress("unused")
    const val UPLOAD_POLL_DELAY_MS: Long = 500L

    // Meta polling interval (millis) — mutable so runtime can adjust polling frequency
    // Default: 15 seconds
    // TIP: Lower this for faster UI updates, but mind battery/network impact. Minimum enforced = 1000ms.
    // メタポーリング間隔（ミリ秒） — 実行時に変更可能
    // デフォルト: 15秒
    // TIP: 値を小さくするとUIの応答性は上がりますが、バッテリやネットワーク負荷に注意してください。下限は 1000ms に制約されています。
    // Two-level meta polling intervals:
    //  - META_POLL_INTERVAL_NORMAL_MS: default interval used during normal operation (slower, less network usage)
    //  - META_POLL_INTERVAL_FAST_MS: used during short-lived waiting windows (e.g. awaiting model after upload)
    @Volatile
    var META_POLL_INTERVAL_NORMAL_MS: Long = 120_000L // default normal: 120s (reduced polling load)

    @Volatile
    var META_POLL_INTERVAL_FAST_MS: Long = 60_000L // fast interval for active waiting: 60s

    // Backwards-compatible alias used in some code paths; defaults to normal interval
    @Volatile
    var META_POLL_INTERVAL_MS: Long = META_POLL_INTERVAL_NORMAL_MS

    /**
     * Set the normal meta polling interval (ms). Minimum enforced to 1000 ms.
     * This updates the legacy `META_POLL_INTERVAL_MS` alias as well.
     */
    fun setMetaPollInterval(ms: Long) {
        val old = META_POLL_INTERVAL_NORMAL_MS
        val adj = if (ms < 1000L) 1000L else ms
        META_POLL_INTERVAL_NORMAL_MS = adj
        META_POLL_INTERVAL_MS = adj
        RealTimeLogger.i(TAG, "setMetaPollInterval requested=$ms adjusted=$adj previous=$old")
    }

    // --- 動的推奨ロジック ---

    /**
     * 合成されるリトライ待ち時間の概算合計を計算します。
     * 初回バックオフ `initialBackoffMs` を基に指数的に増加（倍々）し、
     * 各ステップで `MAX_RETRY_DELAY_MS` を上限にします。maxRetries は "追加リトライ回数" として扱います。
     */
    private fun sumRetryDelays(initialBackoffMs: Long, maxRetries: Int): Long {
        var sum = 0L
        var delay = initialBackoffMs
        for (i in 0 until maxRetries) {
            sum += delay
            // 各ステップの詳細ログ（デバッグ時に有用）
            RealTimeLogger.d(TAG, "sumRetryDelays step=$i delay=$delay runningSum=$sum")
            delay = (delay * 2).coerceAtMost(MAX_RETRY_DELAY_MS)
        }
        RealTimeLogger.d(TAG, "sumRetryDelays(initialBackoffMs=$initialBackoffMs, maxRetries=$maxRetries) = $sum")
        return sum
    }

    /**
     * アップロード後にメタ反映を待つための推奨ポーリングパラメータを計算します。
     * - requestTimeoutMs: 単一アップロード試行のタイムアウト（ms）
     * - initialBackoffMs / maxRetries: クライアント側の再試行設定に基づく遅延見積
     * - serverProcessingMs: サーバが反映に要すると想定する追加バッファ（ms）
     * - preferredDelayMs: UI/ポーリングの目安となる間隔（ms）
     *
     * 戻り値: Pair(attempts, delayMs) — attempts×delayMs を総ポーリング窓の目安にします。
     */
    fun recommendUploadPollingParams(
        requestTimeoutMs: Long = 120_000L,
        initialBackoffMs: Long = 600L,
        maxRetries: Int = 2,
        serverProcessingMs: Long = 2_000L,
        preferredDelayMs: Long = 2000L,
        // Optional cap on attempts to avoid extremely large attempt counts
        maxAttemptsCap: Int? = null
    ): Pair<Int, Long> {
         val retryDelays = sumRetryDelays(initialBackoffMs, maxRetries)
         val totalWindow = requestTimeoutMs + retryDelays + serverProcessingMs
         // attempts は少なくとも1、preferredDelayMs を使って割る
         var attempts = (totalWindow / preferredDelayMs).toInt().coerceAtLeast(1)
         if (maxAttemptsCap != null) attempts = attempts.coerceAtMost(maxAttemptsCap)
         val delayMs = (totalWindow / attempts).coerceAtLeast(500L)

         RealTimeLogger.i(TAG, "recommendUploadPollingParams(requestTimeoutMs=$requestTimeoutMs, initialBackoffMs=$initialBackoffMs, maxRetries=$maxRetries, serverProcessingMs=$serverProcessingMs, preferredDelayMs=$preferredDelayMs, maxAttemptsCap=$maxAttemptsCap) => attempts=$attempts delayMs=$delayMs totalWindow=$totalWindow")

         return Pair(attempts, delayMs)
     }

    /**
     * 推奨される総ポーリングウィンドウ（ミリ秒）を返します（内部計算の合計）。
     */
    @Suppress("unused")
    fun recommendUploadPollingWindowMs(
        requestTimeoutMs: Long = 120_000L,
        initialBackoffMs: Long = 600L,
        maxRetries: Int = 2,
        serverProcessingMs: Long = 2_000L
    ): Long {
        val retryDelays = sumRetryDelays(initialBackoffMs, maxRetries)
        val window = requestTimeoutMs + retryDelays + serverProcessingMs
        RealTimeLogger.d(TAG, "recommendUploadPollingWindowMs(requestTimeoutMs=$requestTimeoutMs, initialBackoffMs=$initialBackoffMs, maxRetries=$maxRetries, serverProcessingMs=$serverProcessingMs) = $window")
        return window
    }
}
