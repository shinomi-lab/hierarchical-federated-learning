package com.example.hfl_experiment.telemetry

import android.app.usage.UsageStatsManager
import android.content.Context
import androidx.tracing.Trace
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import java.util.concurrent.atomic.AtomicInteger

/**
 * HFL向けPerfettoトレースユーティリティ。
 *
 * - beginAsyncSection / endAsyncSection で非同期処理を囲む
 * - Android 9 (API 28) でも NoSuchMethodError が出ないよう androidx.tracing:tracing-ktx に依存
 * - 観測者効果抑制: UsageStats取得はバックグラウンドCoroutineで低頻度実行
 */
object HflTracer {
    private val asyncCookie = AtomicInteger(0)

    // ── 同期トレース（軽量区間向け） ──────────────────────────────────────
    inline fun <T> trace(label: String, block: () -> T): T {
        Trace.beginSection(label)
        try {
            return block()
        } finally {
            Trace.endSection()
        }
    }

    // ── 非同期トレース（学習サイクル・通信処理向け） ─────────────────────
    fun beginAsync(label: String): Int {
        val cookie = asyncCookie.incrementAndGet()
        Trace.beginAsyncSection(label, cookie)
        return cookie
    }

    fun endAsync(label: String, cookie: Int) {
        Trace.endAsyncSection(label, cookie)
    }

    // ── 時刻同期マーカー ─────────────────────────────────────────────────
    fun markSyncStart() {
        Trace.beginSection("SYNC_START_MARKER")
        Trace.endSection()
    }

    // ── UsageStats取得（非同期・低頻度） ─────────────────────────────────
    fun collectUsageStatsAsync(context: Context) {
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val usm = context.getSystemService(Context.USAGE_STATS_SERVICE) as? UsageStatsManager
                    ?: return@launch
                val end = System.currentTimeMillis()
                val start = end - 60_000 // 直近1分
                val stats = usm.queryUsageStats(UsageStatsManager.INTERVAL_BEST, start, end)
                // Traceに情報を刻印（軽量に）
                if (stats.isNotEmpty()) {
                    val foreground = stats.filter { it.totalTimeInForeground > 0 }
                        .sortedByDescending { it.totalTimeInForeground }
                        .take(3)
                    Trace.beginSection("UsageStats_top3")
                    Trace.endSection()
                }
            } catch (_: Exception) {
                // SecurityException or other — silently ignore
            }
        }
    }
}
