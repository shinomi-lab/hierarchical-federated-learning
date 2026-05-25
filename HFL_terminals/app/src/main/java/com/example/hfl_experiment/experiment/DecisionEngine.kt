package com.example.hfl_experiment.experiment

import android.content.Context
import android.util.Log
import com.example.hfl_experiment.notification.NotificationHelper
import com.example.hfl_experiment.util.TerminalSatisfaction
import com.example.hfl_experiment.util.logging.RealTimeLogger

/**
 * DecisionResult: result returned by DecisionEngine.observe()
 */
data class DecisionResult(
    val chosenApId: String,
    val rawScores: List<Float>,
    val emaScores: List<Float>,
    val reason: String
)


class DecisionEngine(
    private val context: Context,
    private val config: Config.ConfigData,
    private val modelLoader: ModelLoader = ModelLoader(),
) {
    private val TAG = "DecisionEngine"

    // EMA state per AP
    private var emaScores: FloatArray = FloatArray(config.apNum) { 0f }

    // consecutive counters for each AP when it appears to be best
    private val consecutiveWins: IntArray = IntArray(config.apNum) { 0 }

    // last switch timestamp (epoch ms)
    private var lastSwitchTs: Long = 0L

    // observation counter to trigger windowed evaluation
    private var observationCounter: Int = 0

    // cooldown flag set by forceCooldown
    @Volatile
    private var forcedCooldownUntil: Long = 0L

    init {
        // load model if available (stub safe)
        try {
            modelLoader.loadModel(context, config.modelFile)
        } catch (e: Exception) {
            Log.w(TAG, "Model load failed: ${e.message}")
        }
    }

    fun forceCooldown(seconds: Long = config.minSwitchSeconds) {
        forcedCooldownUntil = System.currentTimeMillis() + seconds * 1000L
        Log.i(TAG, "forceCooldown set until ${forcedCooldownUntil}")
    }

    /**
     * observe: main API. Returns DecisionResult including chosen AP id and reasoning.
     */
    fun observe(tp: Float, rtt: Float, appOneHot: FloatArray): DecisionResult {
        val start = System.currentTimeMillis()

        // prepare input vector: [tp_mbps, rtt_ms, appOneHot...]
        val input = FloatArray(2 + appOneHot.size)
        input[0] = tp
        input[1] = rtt
        for (i in appOneHot.indices) input[2 + i] = appOneHot[i]

        val raw = try {
            modelLoader.predict(input)
        } catch (e: Exception) {
            Log.w(TAG, "Model predict failed: ${e.message}")
            // fallback: simple heuristic score favoring throughput
            FloatArray(config.apNum) { idx -> if (idx == 0) tp else tp * 0.9f }
        }

        // apply satisfaction correction (reduce score if AP cannot meet needTP/needRTT)
        val corrected = applySatisfactionCorrection(raw, tp, rtt, appOneHot)

        // update EMA each observation
        for (i in corrected.indices) {
            val alpha = config.emaAlpha
            emaScores[i] = if (emaScores[i] == 0f) corrected[i] else emaScores[i] * (1f - alpha) + corrected[i] * alpha
        }

        // increment observation counter and only evaluate switching every cycleWindow observations
        observationCounter = (observationCounter + 1) % config.cycleWindow

        var reason = "none"
        var chosenIdx = -1
        // expose bestIdx to the whole function so we can log it later
        var bestIdx = 0

        if (observationCounter != 0) {
            // Not the window evaluation tick: do not change chosenIdx, return current preferred
            bestIdx = emaScores.indices.maxByOrNull { emaScores[it] } ?: 0
            chosenIdx = getCurrentPreferredIndexOr(bestIdx)
            reason = "pending_window(${observationCounter}/${config.cycleWindow})"
        } else {
            // evaluate at the end of window
            // find best AP by emaScores
            bestIdx = emaScores.indices.maxByOrNull { emaScores[it] } ?: 0
            val bestScore = emaScores[bestIdx]
            // determine second best for delta
            val secondScore = emaScores.withIndex().filter { it.index != bestIdx }.maxOfOrNull { it.value } ?: 0f

            val now = System.currentTimeMillis()
            val sinceLastSwitchSec = if (lastSwitchTs == 0L) Double.MAX_VALUE else (now - lastSwitchTs) / 1000.0

            if (now < forcedCooldownUntil) {
                reason = "forced_cooldown"
                chosenIdx = getCurrentPreferredIndexOr(bestIdx)
            } else if (sinceLastSwitchSec < config.minSwitchSeconds) {
                reason = "min_interval_not_elapsed"
                chosenIdx = getCurrentPreferredIndexOr(bestIdx)
            } else if (bestScore - secondScore < config.deltaThreshold) {
                // not a strong enough improvement -> reset consecutive counts
                reason = "delta_too_small"
                for (i in consecutiveWins.indices) consecutiveWins[i] = 0
                chosenIdx = getCurrentPreferredIndexOr(bestIdx)
            } else {
                // increment consecutive wins for bestIdx, reset others
                for (i in consecutiveWins.indices) {
                    consecutiveWins[i] = if (i == bestIdx) consecutiveWins[i] + 1 else 0
                }

                if (consecutiveWins[bestIdx] >= config.consecutiveThreshold) {
                    // perform switch
                    reason = "threshold_reached"
                    lastSwitchTs = now
                    for (i in consecutiveWins.indices) consecutiveWins[i] = 0
                    chosenIdx = bestIdx
                    // fire a notification suggesting the switch (auto-show)
                    try {
                        val ssid = if (chosenIdx == 0) config.apA.ssid else config.apB.ssid
                        NotificationHelper.showSwitchSuggestion(context, config.getApIdByIndex(chosenIdx), ssid)
                    } catch (_: Exception) {
                        Log.w(TAG, "failed to show notification for chosenIdx=$chosenIdx")
                    }
                } else {
                    reason = "waiting_for_consecutive(${consecutiveWins[bestIdx]}/${config.consecutiveThreshold})"
                    chosenIdx = getCurrentPreferredIndexOr(bestIdx)
                }
            }
        }

        val chosenApId = config.getApIdByIndex(if (chosenIdx >= 0) chosenIdx else 0)

        val duration = System.currentTimeMillis() - start
        Log.d(TAG, "observe: chosen=$chosenApId bestIdx=$bestIdx reason=$reason durMs=${duration}")

        return DecisionResult(
            chosenApId = chosenApId,
            rawScores = raw.toList(),
            emaScores = emaScores.copyOf().toList(),
            reason = reason
        )
    }

    private fun getCurrentPreferredIndexOr(fallback: Int): Int {
        // If we had a previous switch recorded, prefer that AP unless conditions allow change
        val last = config.getLastSelectedIndexSafe(context)
        return last ?: fallback
    }

    private fun applySatisfactionCorrection(raw: FloatArray, tp: Float, rtt: Float, appOneHot: FloatArray): FloatArray {
        val out = raw.copyOf()
        // determine app index from one-hot
        val appIdx = appOneHot.indices.firstOrNull { appOneHot[it] >= 0.5f } ?: 0
        val needTp = (config.needTP["app$appIdx"] ?: 0.0).toFloat()
        val needRtt = (config.needRTT["app$appIdx"] ?: 0).toFloat()

        // Compute terminal satisfaction S using shared utility (centralized spec implementation)
        var appTypeName: String? = null
        try {
            val appTypesField = try { config::class.java.getDeclaredField("appTypes") } catch (_: Exception) { null }
            if (appTypesField != null) {
                appTypesField.isAccessible = true
                val arr = appTypesField.get(config) as? List<*>
                if (arr != null && appIdx < arr.size) appTypeName = arr[appIdx] as? String
            }
        } catch (_: Exception) {
            // ignore
        }
        if (appTypeName == null) {
            appTypeName = when (appIdx) { 1 -> "video"; 2 -> "call"; 0 -> "browser"; else -> "other" }
        }

        val satisfaction = try { TerminalSatisfaction.calculateSatisfaction(appTypeName, tp, rtt, needTp, needRtt) } catch (_: Exception) { 1.0f }

        // Detailed logging for debugging reward calculations
        try {
            RealTimeLogger.i(TAG, "SatisfactionCalc Debug: appIdx=$appIdx appType=$appTypeName metric=${if (appTypeName.lowercase() == "call") "RTT" else "TP"} measured_tp=${tp}Mbps measured_rtt=${rtt}ms need_tp=${needTp}Mbps need_rtt=${needRtt}ms satisfaction=$satisfaction")
        } catch (_: Exception) {}

        // apply satisfaction as multiplicative factor to raw scores
        for (i in out.indices) {
            try {
                out[i] = out[i] * satisfaction
            } catch (_: Exception) {
                // keep original on failure
            }
        }
        return out
    }
}
