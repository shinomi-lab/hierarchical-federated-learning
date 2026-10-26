package com.example.hfl_experiment.network

import java.util.concurrent.atomic.AtomicBoolean

/**
 * Process-wide registry to avoid concurrent "ask"/fetchSendToDevice calls and
 * to maintain a simple cooldown/backoff between attempts.
 */
object FetchInFlightRegistry {
    // true when a fetch/sendToDevice is currently in-flight (either manual or automatic ask)
    val inFlight: AtomicBoolean = AtomicBoolean(false)

    // timestamp of last ask attempt (ms since epoch)
    @Volatile
    var lastAskTs: Long = 0L

    // cooldown in milliseconds to wait before attempting another ask; adaptive backoff on failures
    @Volatile
    var askCooldownMs: Long = 10_000L // default 10s

    // maximum cooldown (e.g., 5 minutes)
    const val MAX_COOLDOWN_MS: Long = 300_000L

    fun recordSuccess() {
        lastAskTs = System.currentTimeMillis()
        askCooldownMs = 10_000L
    }

    fun recordFailure() {
        lastAskTs = System.currentTimeMillis()
        // exponential backoff bounded by MAX_COOLDOWN_MS
        val next = (askCooldownMs * 2).coerceAtMost(MAX_COOLDOWN_MS)
        askCooldownMs = next
    }
}

