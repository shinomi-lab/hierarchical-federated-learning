package com.example.hfl_experiment.telemetry

import android.content.Context
import java.util.concurrent.atomic.AtomicInteger

/** No-op stub — tracing-ktx removed for older device compatibility (API < 29). */
object HflTracer {
    // cookie は区別できるよう単調増加で返す（beginAsync/endAsync の対応付け用。async-hfl の HflTracerTest の契約）
    private val cookieSeq = AtomicInteger(0)

    inline fun <T> trace(label: String, block: () -> T): T = block()
    fun beginAsync(label: String): Int = cookieSeq.incrementAndGet()
    fun endAsync(label: String, cookie: Int) {}
    fun markSyncStart() {}
    fun collectUsageStatsAsync(context: Context) {}
}
