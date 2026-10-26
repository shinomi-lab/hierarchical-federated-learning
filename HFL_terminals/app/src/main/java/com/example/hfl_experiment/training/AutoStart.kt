package com.example.hfl_experiment.training

import android.content.Intent
import com.example.hfl_experiment.AppConfig

/**
 * SP5: adb から試行を開始する入口（無人ランナー用）。ボタン（Fetch Training Data → Start N Cycles）と同じ処理を呼ぶだけで、
 * 学習の中身は変えない。
 *   adb shell am start -n com.example.hfl_experiment/.ui.MainActivity --ez auto_start true [--es run_tag T] [--ei num_cycles N]
 */
const val EXTRA_AUTO_START = "auto_start"
const val EXTRA_RUN_TAG = "run_tag"
const val EXTRA_NUM_CYCLES = "num_cycles"

sealed class AutoStartDecision {
    object Start : AutoStartDecision()
    data class Ignore(val reason: String) : AutoStartDecision()
}

/** 実行中なら無視する。学習済み（trainingFinished）でも新しい試行として始める（試行の前にアプリは force-stop される）。 */
fun decideAutoStart(isRunning: Boolean, trainingFinished: Boolean): AutoStartDecision =
    if (isRunning) AutoStartDecision.Ignore("already_running") else AutoStartDecision.Start

data class AutoStartRequest(val runTag: String?, val numCycles: Int?)

/** intent に auto_start があれば読み取り、extra を消して返す（同じ intent を 2 回処理しない）。範囲外のサイクル数は null。 */
fun takeAutoStartRequest(intent: Intent?): AutoStartRequest? {
    if (intent == null || !intent.getBooleanExtra(EXTRA_AUTO_START, false)) return null
    val runTag = intent.getStringExtra(EXTRA_RUN_TAG)?.takeIf { it.isNotBlank() }
    val n = if (intent.hasExtra(EXTRA_NUM_CYCLES)) intent.getIntExtra(EXTRA_NUM_CYCLES, 0) else null
    intent.removeExtra(EXTRA_AUTO_START)
    intent.removeExtra(EXTRA_RUN_TAG)
    intent.removeExtra(EXTRA_NUM_CYCLES)
    return AutoStartRequest(runTag = runTag, numCycles = n?.takeIf { AppConfig.isValidNumCycles(it) })
}

data class RunArgs(val inputSize: Int, val hiddenSize: Int, val outputSize: Int, val epochs: Int, val autoUpload: Boolean)

/** ボタン（MainActivity の onStartTraining）と自動開始で同じ引数を使う。 */
fun buildRunArgs(appConfigsSize: Int?): RunArgs {
    val tpRttFeatures = 2
    val appNumMax = appConfigsSize ?: AppConfig.APP_CAT_COUNT
    return RunArgs(inputSize = tpRttFeatures + appNumMax, hiddenSize = 32, outputSize = AppConfig.AP_COUNT,
        epochs = 5, autoUpload = true)  // 学習完了時の自動アップロードは必須
}

// ── SP5 Task 10: セッション完了イベント ──────────────────────────────────

/** セッションで固定した値（runFiveCycles の終わりに控える）と結果。result は ok / error / cancelled。 */
data class SessionCompleteInfo(
    val terminalId: String,
    val runId: String,
    val runTag: String?,
    val result: String,
    val reason: String?,
    val hflMode: String,
    val hflModeSource: String,
    val numCycles: Int,
    val numCyclesSource: String,
    val baselineMode: String,
    val cyclesCompleted: Int,
    val afterSwitchConfirmed: String,  // true / false / unknown（無人）、not_measured（手動。After はボタンで別に測る）
    val startedAtMs: Long,
    val endedAtMs: Long,
)

/** 実験イベント session_complete（未送信キュー経由でエッジ→中央へ）。無人ランナーは run_tag ごとに台数分そろうのを待つ。 */
fun buildSessionCompleteEvent(i: SessionCompleteInfo): org.json.JSONObject {
    val n = org.json.JSONObject.NULL
    return org.json.JSONObject()
        .put("kind", "experiment_event").put("type", "session_complete")
        .put("terminal_id", i.terminalId).put("run_id", i.runId).put("run_tag", i.runTag ?: n)
        .put("result", i.result).put("reason", i.reason ?: n)
        .put("hfl_mode", i.hflMode).put("hfl_mode_source", i.hflModeSource)
        .put("num_cycles", i.numCycles).put("num_cycles_source", i.numCyclesSource)
        .put("baseline_mode", i.baselineMode).put("cycles_completed", i.cyclesCompleted)
        .put("after_switch_confirmed", i.afterSwitchConfirmed)
        .put("started_at_ms", i.startedAtMs).put("ended_at_ms", i.endedAtMs)
        .put("timestamp_ms", i.endedAtMs)
}

// ── SP5 Task 11: 無人モードの After の前に AP の切り替えを待つ ────────────────

const val AFTER_SWITCH_WAIT_MS = 60_000L

/**
 * fix-audit-20261026 N2: After（5 回・3 秒おき）を測る前の安定待ち。切り替えたかどうかに関わらず常に待つ。
 * 10/22 の実機で、切り替えた回の After は 3 回目に大きく落ち、切替直後の影響が混ざっていた。
 */
const val AFTER_SETTLE_MS = 20_000L

/** 安定待ちをしてから、After の開始時の AP を読む（順序をテストで固定する）。戻り値は（待った ms, 開始時の AP）。 */
suspend fun <T> settleThenReadAp(settleMs: Long, delayFn: suspend (Long) -> Unit, readAp: () -> T): Pair<Long, T> {
    delayFn(settleMs)
    return settleMs to readAp()
}

/** After を補完する行に、安定待ちの時間を入れる（以前のデータには無い＝条件差）。 */
fun putAfterSettle(row: org.json.JSONObject, settleMs: Long): org.json.JSONObject = row.put("after_settle_ms", settleMs)

/** Random のセッションの要約行に足す項目（選んだ AP）。Random 以外は null（項目を足さない）。 */
fun randomSessionFields(mode: String, choice: Int?): org.json.JSONObject? =
    if (mode != "random") null else org.json.JSONObject().put("random_choice_ap", choice ?: org.json.JSONObject.NULL)
const val AFTER_SWITCH_POLL_MS = 2_000L

/** 切り替え先が無い（切り替えを提案していない）か、一致すれば "true"。タイムアウトで不一致なら "false"。SSID が取れなければ "unknown"。 */
fun afterSwitchStatus(currentSsid: String?, targetSsid: String?, timedOut: Boolean): String = when {
    targetSsid.isNullOrBlank() -> "true"
    currentSsid.isNullOrBlank() -> "unknown"
    currentSsid == targetSsid -> "true"
    timedOut -> "false"
    else -> "unknown"
}

/**
 * 切替の確認を AP 番号で行う版（ap-detection-fallback）。SSID が隠される端末でも、サブネットで判定した AP と提案の AP を比べられる。
 * 提案が無い（targetAp が null）なら "true"。現在の AP が判定できなければ "unknown"。一致で "true"、タイムアウトで不一致なら "false"。
 */
fun afterSwitchStatusByAp(current: ConnectedAp, targetAp: Int?, timedOut: Boolean): String = when {
    targetAp == null -> "true"
    current.ap == null -> "unknown"
    current.ap == targetAp -> "true"
    timedOut -> "false"
    else -> "unknown"
}

/** このセッションで最後に切り替えを提案した AP の SSID（ベースラインの DecisionEngine と、サーバの switch_ap）。 */
object SwitchTarget {
    @Volatile var ssid: String? = null
}
