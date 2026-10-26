package com.example.hfl_experiment.experiment

import android.content.Context
import android.util.Log
import com.example.hfl_experiment.AppConfig
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


/**
 * record-connected-ap: ap_states の 1 つの AP。tp / rtt は接続数から計算した模擬値、measured* はその AP に繋がっている端末の
 * 実測の中央値（無ければ null）。n は接続数。
 */
data class ApStateInfo(
    val tp: Float,
    val rtt: Float,
    val n: Float,
    val measuredTp: Float? = null,
    val measuredRtt: Float? = null,
)

/** Greedy が実際に使う TP・RTT と出所（"measured" = 実測を 1 つでも使った／"model" = 模擬値のみ）。 */
data class EffectiveApState(val tp: Float, val rtt: Float, val source: String)

/** 実測があればそれ、無ければ模擬値（TP と RTT を別々に判断する）。 */
fun effectiveApState(info: ApStateInfo): EffectiveApState {
    val tp = info.measuredTp ?: info.tp
    val rtt = info.measuredRtt ?: info.rtt
    val measured = info.measuredTp != null || info.measuredRtt != null
    return EffectiveApState(tp, rtt, if (measured) "measured" else "model")
}


class DecisionEngine(
    private val context: Context,
    private val config: Config.ConfigData,
    private val modelLoader: ModelLoader = ModelLoader(),
    // app.json のアプリ定義（indicator・要求値）。null なら旧 ap_config.json の値で判定
    private val appConfigs: List<com.example.hfl_experiment.model.AppConfig>? = null,
    // Random ベースラインの乱数（テストで固定する）
    private val random: kotlin.random.Random = kotlin.random.Random.Default,
) {
    private val TAG = "DecisionEngine"

    /**
     * fix-audit-20261026 N3: Random はセッションの最初に 1 回だけ AP を決め、セッションの間は同じ AP を返す。
     * 以前は計測ごとに引き直していたため、1 回でも AP1 が出るとそこを提案・After の目標にし、ほぼ常に AP1 になっていた。
     * null = まだ決めていない（セッションの開始で [resetSessionRandomChoice] を呼ぶ）。
     */
    @Volatile var sessionRandomChoice: Int? = null
        private set

    fun resetSessionRandomChoice() { sessionRandomChoice = null }

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
    /**
     * Baseline-aware AP state holder for greedy mode.
     * Set externally before calling observe() when BASELINE_MODE == "greedy".
     * Map of AP index -> [tp_mbps, rtt_ms, n_connected]
     */
    var apStatesForGreedy: Map<Int, FloatArray>? = null

    /**
     * record-connected-ap: 実測つきの AP 状態（AP 番号 -> ApStateInfo）。あれば apStatesForGreedy より優先する。
     * 比べるのは ap_config.json の AP（apA・apB = 番号 0 .. apNum-1）だけ。サーバが他の AP（旧形式の AP2 など）を返しても無視する。
     */
    var apStateInfosForGreedy: Map<Int, ApStateInfo>? = null

    /** 直近の Greedy の判定で、AP ごとに実測（"measured"）と模擬値（"model"）のどちらを使ったか。 */
    @Volatile var lastGreedySources: Map<Int, String> = emptyMap()

    fun observe(tp: Float, rtt: Float, appOneHot: FloatArray): DecisionResult {
        val start = System.currentTimeMillis()
        val baselineMode = AppConfig.BASELINE_MODE

        // Baseline modes: bypass model inference entirely
        if (baselineMode != "none") {
            val chosenIdx = when (baselineMode) {
                "fixed" -> 0
                "random" -> sessionRandomChoice ?: random.nextInt(config.apNum).also { sessionRandomChoice = it }
                "fixed_ap1" -> 1   // fix-audit-20261026 N2: AP1 の定常の満足度を測るベースライン
                "greedy" -> pickGreedy(appOneHot)
                // central: 実測では AP を決めない（推奨は observeCentral で中央の応答から受け取る）。現在の AP のまま
                "central", "hfl_rec" -> getCurrentPreferredIndexOr(0)
                else -> 0
            }
            val chosenApId = config.getApIdByIndex(chosenIdx.coerceIn(0, config.apNum - 1))
            val duration = System.currentTimeMillis() - start
            RealTimeLogger.i(TAG, "observe(baseline=$baselineMode): chosen=$chosenApId idx=$chosenIdx durMs=$duration")
            return DecisionResult(
                chosenApId = chosenApId,
                rawScores = List(config.apNum) { 0f },
                emaScores = List(config.apNum) { 0f },
                reason = "baseline_$baselineMode"
            )
        }

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

    /**
     * central モード: 中央の応答を DecisionResult にする。端末内モデルは使わない（modelLoader を呼ばない）。
     * 成功なら中央の推奨 AP、失敗（タイムアウト等）なら現在の AP のまま（reason に失敗の種類）。
     * 推奨 AP は記録のためで、端末は自動では切り替えない（AP の切替は実験者が手で行う）。
     */
    fun observeCentral(outcome: CentralOutcome): DecisionResult {
        val ap = outcome.chosenAp
        return if (outcome.status == CentralClient.STATUS_OK && ap != null) {
            val scores = (outcome.scores ?: emptyList()).map { it.toFloat() }
            DecisionResult(
                chosenApId = config.getApIdByIndex(ap.coerceIn(0, config.apNum - 1)),
                rawScores = scores,
                emaScores = List(config.apNum) { 0f },
                reason = "central_ok"
            )
        } else {
            DecisionResult(
                chosenApId = config.getApIdByIndex(getCurrentPreferredIndexOr(0).coerceIn(0, config.apNum - 1)),
                rawScores = List(config.apNum) { 0f },
                emaScores = List(config.apNum) { 0f },
                reason = "central_${outcome.status}"
            )
        }
    }

    private fun getCurrentPreferredIndexOr(fallback: Int): Int {
        // If we had a previous switch recorded, prefer that AP unless conditions allow change
        val last = config.getLastSelectedIndexSafe(context)
        return last ?: fallback
    }

    /**
     * Greedy AP selection: pick the AP with highest satisfaction for this terminal's app type.
     * Uses apStatesForGreedy (AP index -> [tp, rtt, n]) set before observe().
     */
    internal fun pickGreedyForTest(appOneHot: FloatArray): Int = pickGreedy(appOneHot)

    private fun pickGreedy(appOneHot: FloatArray): Int {
        val states: Map<Int, ApStateInfo>? = apStateInfosForGreedy
            ?: apStatesForGreedy?.mapValues { (_, v) -> ApStateInfo(tp = v.getOrElse(0) { 0f }, rtt = v.getOrElse(1) { 0f }, n = v.getOrElse(2) { 0f }) }
        // 実験で使う AP だけ（ap_config.json の apNum = 2 → 番号 0・1）。AP2 などは比べない
        val candidates = states?.filterKeys { it in 0 until config.apNum }
        if (candidates.isNullOrEmpty()) {
            RealTimeLogger.w(TAG, "pickGreedy: no AP states available, defaulting to AP0")
            lastGreedySources = emptyMap()
            return 0
        }
        val appIdx = appOneHot.indices.firstOrNull { appOneHot[it] >= 0.5f } ?: 0
        val (indicator, needTp, needRtt) = appRequirement(appIdx, appConfigs, config)
        val appTypeName = "app$appIdx($indicator)"

        var bestIdx = 0
        var bestSat = -1f
        val sources = linkedMapOf<Int, String>()
        for ((apIdx, info) in candidates.toSortedMap()) {
            val eff = effectiveApState(info)
            sources[apIdx] = eff.source
            val sat = try {
                TerminalSatisfaction.calculateByIndicator(indicator, eff.tp, eff.rtt, needTp, needRtt)
            } catch (_: Exception) { 0f }
            RealTimeLogger.d(TAG, "pickGreedy: AP$apIdx tp=${eff.tp} rtt=${eff.rtt} sat=$sat src=${eff.source} (model tp=${info.tp} rtt=${info.rtt}; app=$appTypeName)")
            if (sat > bestSat) {
                bestSat = sat
                bestIdx = apIdx
            }
        }
        lastGreedySources = sources
        RealTimeLogger.i(TAG, "pickGreedy: chose AP$bestIdx with sat=$bestSat for app=$appTypeName sources=$sources")
        return bestIdx
    }

    private fun applySatisfactionCorrection(raw: FloatArray, tp: Float, rtt: Float, appOneHot: FloatArray): FloatArray {
        val out = raw.copyOf()
        // determine app index from one-hot
        val appIdx = appOneHot.indices.firstOrNull { appOneHot[it] >= 0.5f } ?: 0
        val (indicator, needTp, needRtt) = appRequirement(appIdx, appConfigs, config)
        val appTypeName = "app$appIdx($indicator)"

        val satisfaction = try { TerminalSatisfaction.calculateByIndicator(indicator, tp, rtt, needTp, needRtt) } catch (_: Exception) { 1.0f }

        // Detailed logging for debugging reward calculations
        try {
            RealTimeLogger.i(TAG, "SatisfactionCalc Debug: appIdx=$appIdx appType=$appTypeName metric=${if (indicator == "rtt") "RTT" else "TP"} measured_tp=${tp}Mbps measured_rtt=${rtt}ms need_tp=${needTp}Mbps need_rtt=${needRtt}ms satisfaction=$satisfaction")
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

/**
 * アプリの判定基準と要求値 (indicator, needTP, needRTT)。app.json（appConfigs）を優先し、
 * 無い場合のみ旧 ap_config.json の値（通話 app2 のみ RTT 優先）を使う。配信 は app.json で indicator=rtt。
 */
internal fun appRequirement(
    appIdx: Int,
    appConfigs: List<com.example.hfl_experiment.model.AppConfig>?,
    config: Config.ConfigData,
): Triple<String, Float, Float> {
    appConfigs?.getOrNull(appIdx)?.let { a -> return Triple(a.indicator.trim().lowercase(), a.needTP.toFloat(), a.needRTT.toFloat()) }
    val needTp = (config.needTP["app$appIdx"] ?: 0.0).toFloat()
    val needRtt = (config.needRTT["app$appIdx"] ?: 0).toFloat()
    return Triple(if (appIdx == 2) "rtt" else "tp", needTp, needRtt)
}

