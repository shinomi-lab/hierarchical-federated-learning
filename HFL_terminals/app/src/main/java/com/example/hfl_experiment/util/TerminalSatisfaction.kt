package com.example.hfl_experiment.util

import com.example.hfl_experiment.util.logging.RealTimeLogger

/**
 * Terminal satisfaction helper.
 * Implements the spec:
 * - app_type: browser, video, call, other
 * - TP-priority: S = TP_link / TP_need
 * - RTT-priority: S = RTT_need / RTT_link
 * - Clip to (0, 1]
 */
object TerminalSatisfaction {
    private const val EPS = 1e-6f
    private const val TAG = "TerminalSatisfaction"

    fun calculateSatisfaction(appType: String?, tpLink: Float?, rttLink: Float?, tpNeed: Float?, rttNeed: Float?): Float {
        val at = appType?.trim()?.lowercase() ?: ""
        val tpPrior = setOf("browser", "video", "other")
        val rttPrior = setOf("call")

        try {
            // Normalize units for RTT: prefer milliseconds (ms)
            var measuredRttMs = rttLink ?: 0f
            var needRttMs = rttNeed ?: 0f
            // Heuristic: if need is small (<=10) and measured is large (>10) assume need was in seconds and convert
            if (needRttMs > 0f && needRttMs <= 10f && measuredRttMs > 10f) {
                RealTimeLogger.w(TAG, "RTT unit heuristic: converting needRtt ${needRttMs} (assumed sec) to ms")
                needRttMs *= 1000f
            }
            // If measured looks like seconds (<=10) but need is large in ms, convert measured
            if (measuredRttMs > 0f && measuredRttMs <= 10f && needRttMs > 10f) {
                RealTimeLogger.w(TAG, "RTT unit heuristic: converting measuredRtt ${measuredRttMs} (assumed sec) to ms")
                measuredRttMs *= 1000f
            }

            // For TP we expect Mbps for both tpLink and tpNeed — no conversion by default, but guard against zero/NaN
            val measuredTp = tpLink ?: 0f
            val needTp = tpNeed ?: 0f

            val raw: Float = if (at in rttPrior) {
                // RTT-priority: smaller RTT is better: S = RTT_need / RTT_link
                val denom = measuredRttMs
                if (denom <= 0f) {
                    RealTimeLogger.w(TAG, "RTT denom zero or missing: measuredRttMs=$measuredRttMs needRttMs=$needRttMs")
                    0f
                } else {
                    needRttMs / denom
                }
            } else {
                // TP-priority default: S = TP_link / TP_need
                if (needTp <= 0f) {
                    RealTimeLogger.w(TAG, "TP need is zero or missing: measuredTp=$measuredTp needTp=$needTp")
                    1f
                } else {
                    measuredTp / needTp
                }
            }

            // Logging all relevant values for debugging
            try {
                val metric = if (at in rttPrior) "RTT" else "TP"
                val needVal = if (metric == "RTT") needRttMs.toDouble() else needTp.toDouble()
                val measuredVal = if (metric == "RTT") measuredRttMs.toDouble() else measuredTp.toDouble()
                RealTimeLogger.i(TAG, "calcSatisfaction: appType=$appType metric=$metric need=$needVal measured=$measuredVal rawScore=$raw")
            } catch (_: Exception) {}

            var s = if (raw.isNaN()) EPS else raw
            if (s <= 0f) s = EPS
            if (s > 1f) s = 1f

            try { RealTimeLogger.i(TAG, "calcSatisfaction: finalReward=$s appType=$appType") } catch (_: Exception) {}
            return s
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "calculateSatisfaction exception: ${e.message}")
            return EPS
        }
    }
}
