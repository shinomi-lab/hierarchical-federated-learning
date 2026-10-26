package com.example.hfl_experiment.training

import android.content.Context
import com.example.hfl_experiment.experiment.ExperimentContext
import com.example.hfl_experiment.util.PerfLogger
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream

/**
 * 端末ローカルの学習メトリクス永続化ストア。
 * filesDir/training_metrics.jsonl に 1 行 1 JSON で追記する。
 *
 * データ消失防止の設計：
 * - clearAll() はサーバーへのアップロード成功が確認された後にのみ呼ぶこと。
 * - run_id ごとに別ファイルへ書き込むことで、複数セッションのデータが混在しない。
 * - 共通ファイル training_metrics.jsonl への追記は後方互換のために維持する。
 */
object TrainingMetricsStore {
    private const val LEGACY_FILE = "training_metrics.jsonl"
    private const val LEDGER_FILE = "metrics_ledger.jsonl"   // 追記のみ・削除しない（事後照合の正本）
    private const val OUTBOX_FILE = "metrics_outbox.jsonl"   // 未送信キュー
    private lateinit var baseDir: File
    private var initialized = false

    fun init(context: Context) {
        if (initialized) return
        initDir(context.filesDir)
    }

    internal fun initDir(dir: File) {
        synchronized(this) {
            baseDir = dir
            initialized = true
            val legacy = File(dir, LEGACY_FILE)
            if (legacy.exists()) {
                legacy.readLines().filter { it.isNotBlank() }.forEach { line ->
                    try { recordRowLocked(JSONObject(line)) } catch (_: Exception) {}
                }
                legacy.delete()
            }
        }
    }

    fun record(metric: TrainingMetric): String? = if (!initialized) null else recordRow(metric.toJson())

    /** 台帳へ追記し、未送信キューの同じ data_id を置き換える。data_id を返す。 */
    fun recordRow(row: JSONObject): String = synchronized(this) { recordRowLocked(row) }

    private fun recordRowLocked(row: JSONObject): String {
        if (!row.has("data_id") || row.optString("data_id").isBlank()) row.put("data_id", java.util.UUID.randomUUID().toString())
        val id = row.getString("data_id")
        val line = row.toString() + "\n"
        FileOutputStream(File(baseDir, LEDGER_FILE), true).use { it.write(line.toByteArray()) }
        val kept = readJsonl(File(baseDir, OUTBOX_FILE)).filter { it.optString("data_id") != id }
        writeJsonl(File(baseDir, OUTBOX_FILE), kept + row)
        PerfLogger.append("TrainingMetrics", "recorded data_id=$id cycle=${row.optInt("cycle_number", -1)}")
        return id
    }

    fun pending(): List<JSONObject> = synchronized(this) {
        if (!initialized) emptyList() else readJsonl(File(baseDir, OUTBOX_FILE))
    }

    /** 送信成功した data_id の行だけを未送信キューから取り除く（読み直してから書き戻す）。 */
    fun markSent(dataId: String) {
        synchronized(this) {
            if (!initialized) return
            val f = File(baseDir, OUTBOX_FILE)
            writeJsonl(f, readJsonl(f).filter { it.optString("data_id") != dataId })
        }
    }

    /** 台帳を data_id ごとに最後の行で返す（初出順）。 */
    fun latestRows(): Map<String, JSONObject> = synchronized(this) {
        val out = LinkedHashMap<String, JSONObject>()
        if (initialized) readJsonl(File(baseDir, LEDGER_FILE)).forEach { out[it.optString("data_id")] = it }
        out
    }

    /**
     * After の補完先: その run_id の最終サイクルの行（ベースラインのセッションでは要約行 row_kind=baseline_session）。
     * 実験イベント（kind=experiment_event。hfl_mode_mismatch 等も同じ run_id を持つ）は対象にしない（SP3d 3.2）。
     */
    fun lastCycleRow(runId: String): JSONObject? =
        latestRows().values
            .filter { it.optString("run_id") == runId && it.optString("kind") != "experiment_event" }
            .maxByOrNull { it.optInt("cycle_number", -1) }

    /** 台帳の全文（テレメトリの直近メトリクス表示用）。 */
    fun listAll(): String? = synchronized(this) {
        if (!initialized) null else File(baseDir, LEDGER_FILE).takeIf { it.exists() }?.readText()
    }

    private fun readJsonl(f: File): List<JSONObject> =
        if (!f.exists()) emptyList() else f.readLines().filter { it.isNotBlank() }.mapNotNull { runCatching { JSONObject(it) }.getOrNull() }

    private fun writeJsonl(f: File, rows: List<JSONObject>) {
        val tmp = File(f.parentFile, f.name + ".tmp")
        tmp.writeText(rows.joinToString("") { it.toString() + "\n" })
        if (!tmp.renameTo(f)) { f.delete(); tmp.renameTo(f) }
    }

    data class TrainingMetric(
        val terminalId: String,
        val timestamp: Long,
        val totalLocalMs: Long,
        val epochDurations: List<Long>,
        val inputSize: Int?,
        val hiddenSize: Int?,
        val outputSize: Int?,
        val dataId: String? = null,
        val round: Int? = null,
        val accuracy: Double? = null,
        val loss: Double? = null,
        // 追加フィールド（研究データ採取）
        val runId: String? = null,
        val experimentGroup: String? = null,
        val cycleNumber: Int? = null,       // セッション中の何サイクル目か（1 始まり）
        val epochCount: Int? = null,        // 実際に完了したエポック数
        val valAccuracy: Double? = null,    // 検証精度
        val valLoss: Double? = null,        // 検証損失
        val trainAccuracy: Double? = null,  // 訓練精度
        val trainLoss: Double? = null,      // 訓練損失
        val dataSamplesCount: Int? = null,  // 学習に使用したサンプル数
        val batteryAtStart: Double? = null, // 学習開始時のバッテリー残量
        val batteryAtEnd: Double? = null,   // 学習終了時のバッテリー残量
        val memUsedMbAtStart: Double? = null, // 学習開始時のメモリ使用量
        val memUsedMbAtEnd: Double? = null,   // 学習終了時のメモリ使用量
        val weightNorm: Double? = null,     // アップロードした重みの L2 ノルム（モデルドリフト分析用）
        val networkTypeAtUpload: String? = null, // アップロード時のネットワーク種別
        val uploadDurationMs: Long? = null, // アップロードにかかった時間
        val uploadSuccess: Boolean? = null, // アップロード成否
        val satisfactionBefore: Double? = null,
        val satisfactionAfter: Double? = null,
        val appType: String? = null,
        val appIndex: Int? = null,
        val modelVersion: String? = null,
        val tpMeasured: Double? = null,
        val rttMeasured: Double? = null,
        val valAccuracyMean: Double? = null,
        val valLossMean: Double? = null,
        val epochValAccuracy: List<Double>? = null,
        val epochValLoss: List<Double>? = null,
        val satisfactionCycle: Double? = null,
        val virtualRouterId: String? = null
    ) {
        fun toJson(): JSONObject {
            val jo = JSONObject()
            jo.put("terminal_id", terminalId)
            jo.put("timestamp_ms", timestamp)
            jo.put("total_local_ms", totalLocalMs)
            jo.put("epoch_durations_ms", epochDurations)
            inputSize?.let { jo.put("input_size", it) }
            hiddenSize?.let { jo.put("hidden_size", it) }
            outputSize?.let { jo.put("output_size", it) }
            dataId?.let { jo.put("data_id", it) }
            round?.let { jo.put("round", it) }
            accuracy?.let { jo.put("accuracy", it) }
            loss?.let { jo.put("loss", it) }
            // 研究フィールド
            runId?.let { jo.put("run_id", it) }
            experimentGroup?.let { jo.put("experiment_group", it) }
            cycleNumber?.let { jo.put("cycle_number", it) }
            epochCount?.let { jo.put("epoch_count", it) }
            valAccuracy?.let { jo.put("val_accuracy", it) }
            valLoss?.let { jo.put("val_loss", it) }
            trainAccuracy?.let { jo.put("train_accuracy", it) }
            trainLoss?.let { jo.put("train_loss", it) }
            dataSamplesCount?.let { jo.put("data_samples_count", it) }
            batteryAtStart?.let { jo.put("battery_at_start", it) }
            batteryAtEnd?.let { jo.put("battery_at_end", it) }
            memUsedMbAtStart?.let { jo.put("mem_used_mb_at_start", it) }
            memUsedMbAtEnd?.let { jo.put("mem_used_mb_at_end", it) }
            weightNorm?.let { jo.put("weight_norm", it) }
            networkTypeAtUpload?.let { jo.put("network_type_at_upload", it) }
            uploadDurationMs?.let { jo.put("upload_duration_ms", it) }
            uploadSuccess?.let { jo.put("upload_success", it) }
            satisfactionBefore?.let { jo.put("satisfaction_before", it) }
            satisfactionAfter?.let { jo.put("satisfaction_after", it) }
            appType?.let { jo.put("app_type", it) }
            appIndex?.let { jo.put("app_index", it) }
            modelVersion?.let { jo.put("model_version", it) }
            tpMeasured?.let { jo.put("tp_measured_mbps", it) }
            rttMeasured?.let { jo.put("rtt_measured_ms", it) }
            valAccuracyMean?.let { jo.put("val_accuracy_mean", it) }
            valLossMean?.let { jo.put("val_loss_mean", it) }
            epochValAccuracy?.let { jo.put("epoch_val_accuracy", org.json.JSONArray(it)) }
            epochValLoss?.let { jo.put("epoch_val_loss", org.json.JSONArray(it)) }
            satisfactionCycle?.let { jo.put("satisfaction_cycle", it) }
            virtualRouterId?.let { jo.put("virtual_router_id", it) }
            return jo
        }
    }
}

data class EpochSummary(
    val lastAcc: Double?, val lastLoss: Double?, val meanAcc: Double?, val meanLoss: Double?,
    val accs: List<Double>, val losses: List<Double>
)

/** エポック別の検証値から、最終値（アップロードする重みの性能）と平均を作る。 */
fun summarizeEpochs(valAcc: List<Double?>, valLoss: List<Double?>): EpochSummary {
    val accs = valAcc.filterNotNull()
    val losses = valLoss.filterNotNull()
    return EpochSummary(
        lastAcc = accs.lastOrNull(), lastLoss = losses.lastOrNull(),
        meanAcc = accs.takeIf { it.isNotEmpty() }?.average(), meanLoss = losses.takeIf { it.isNotEmpty() }?.average(),
        accs = accs, losses = losses
    )
}

data class UploadOutcome(val success: Boolean, val durationMs: Long?, val networkType: String?)

/** アップロード後に決まる値を同じ data_id の行に補完したコピーを返す。null の引数は触らない。 */
fun completeCycleMetric(row: JSONObject, upload: UploadOutcome?, satisfactionCycle: Double?, tpMbps: Double?, rttMs: Double?): JSONObject {
    val out = JSONObject(row.toString())
    upload?.let {
        out.put("upload_success", it.success)
        it.durationMs?.let { d -> out.put("upload_duration_ms", d) }
        it.networkType?.let { n -> out.put("network_type_at_upload", n) }
    }
    satisfactionCycle?.let { out.put("satisfaction_cycle", it) }
    tpMbps?.let { out.put("tp_measured_mbps", it) }
    rttMs?.let { out.put("rtt_measured_ms", it) }
    out.put("timestamp_ms", System.currentTimeMillis())
    return out
}

/** 重みアップロードの client_meta に metric を同梱する。上限超過時はエポック配列を外し、それでも超えれば同梱しない。 */
fun embedMetricInClientMeta(clientMeta: JSONObject, row: JSONObject, limitBytes: Int = 8192): JSONObject {
    val out = JSONObject(clientMeta.toString())
    out.put("metric", JSONObject(row.toString()))
    if (out.toString().toByteArray(Charsets.UTF_8).size <= limitBytes) return out
    val slim = JSONObject(row.toString()).apply { remove("epoch_val_accuracy"); remove("epoch_val_loss") }
    out.put("metric", slim)
    if (out.toString().toByteArray(Charsets.UTF_8).size <= limitBytes) return out
    out.remove("metric")
    return out
}

fun resolveAppType(appType: String?): String = appType?.takeIf { it.isNotBlank() } ?: "unknown"

/** SP3d: セッションのサイクル数と出所（"server"/"app"）を足した新しい行を返す（元の行は変えない）。 */
fun withSessionSettings(row: JSONObject, numCycles: Int, numCyclesSource: String): JSONObject =
    JSONObject(row.toString()).put("num_cycles", numCycles).put("num_cycles_source", numCyclesSource)

/** SP3d: 学習セッション完了時の表示（サイクル数はセッションで固定した値）。 */
fun cyclesCompletedMessage(numCycles: Int): String =
    "${numCycles}サイクル学習完了 — AP切り替え後に「満足度(After)を計測」を押してください"

/** SP3d: 満足度 After の計測回数と間隔（ボタン 1 回で AFTER_SAMPLES 回、AFTER_INTERVAL_MS おき）。 */
const val AFTER_SAMPLES = 5
const val AFTER_INTERVAL_MS = 3000L

/** SP3d: After の要約。samples は計測順で、失敗した回は null。mean/std は有効値（null・NaN を除く）から。 */
data class AfterSummary(val mean: Double?, val std: Double?, val n: Int, val samples: List<Double?>)

/** 有効値の平均と不偏標準偏差（n−1）。有効値が 0 個なら mean/std とも null、1 個なら std は null。 */
fun summarizeAfterSamples(samples: List<Double?>): AfterSummary {
    val valid = samples.filterNotNull().filter { !it.isNaN() }
    val n = valid.size
    val mean = if (n > 0) valid.average() else null
    val std = if (n > 1) Math.sqrt(valid.sumOf { (it - mean!!) * (it - mean) } / (n - 1)) else null
    return AfterSummary(mean, std, n, samples.map { if (it != null && it.isNaN()) null else it })
}

/** 行（実験レコード等）に After の全値・標準偏差・件数を入れる（未計測なら null）。satisfaction_after は呼び出し側。 */
fun putAfterSummary(jo: JSONObject, s: AfterSummary?): JSONObject {
    jo.put("satisfaction_after_samples", s?.let { org.json.JSONArray().apply { it.samples.forEach { v -> put(v ?: JSONObject.NULL) } } } ?: JSONObject.NULL)
    jo.put("satisfaction_after_std", s?.std ?: JSONObject.NULL)
    jo.put("satisfaction_after_n", s?.n ?: JSONObject.NULL)
    return jo
}

/** SP3d: 最終サイクル（またはベースラインの要約）の行に、同じ data_id で After を補完した新しい行を返す（元の行は変えない）。 */
fun completeAfterMetric(row: JSONObject, s: AfterSummary, timestampMs: Long): JSONObject {
    val out = JSONObject(row.toString())
    out.put("satisfaction_after", s.mean ?: JSONObject.NULL)
    putAfterSummary(out, s)
    out.put("timestamp_ms", timestampMs)
    return out
}

/** SP3d 3.2: ベースラインのセッション要約行の元になる、セッションで固定した値。 */
data class BaselineSessionInfo(
    val runId: String,
    val terminalId: String,
    val experimentGroup: String?,
    val baselineMode: String,
    val baselineModeSource: String,
    val hflMode: String,
    val hflModeSource: String,
    val numCycles: Int,
    val numCyclesSource: String,
    val plannedMeasurements: Int,
    val appType: String,
    val appIndex: Int,
    val virtualRouterId: String?,
    val timestampMs: Long,
)

/**
 * SP3d 3.2: ベースラインのセッション要約行（row_kind=baseline_session）。サイクルの行を作らないベースラインでも、
 * この行を SP-M の台帳・未送信キューで中央へ送り、After をこの行に同じ data_id で補完する。
 * cycle_number と学習の値（accuracy 等）は入れない。ベースライン計測の値は計測順（失敗は null）。
 */
fun buildBaselineSessionRow(
    info: BaselineSessionInfo, satisfactionBefore: Double?, samples: List<Double?>,
    central: JSONObject? = null,   // central モードのときだけ central_* 項目（experiment/CentralSession.kt）を足す
): JSONObject {
    val s = summarizeAfterSamples(samples)   // 平均・件数の求め方は After と同じ（null・NaN を除く）
    val row = JSONObject()
        .put("data_id", java.util.UUID.randomUUID().toString())
        .put("row_kind", "baseline_session")
        .put("run_id", info.runId)
        .put("terminal_id", info.terminalId)
        .put("experiment_group", info.experimentGroup ?: JSONObject.NULL)
        .put("timestamp_ms", info.timestampMs)
        .put("app_type", info.appType)
        .put("app_index", info.appIndex)
        .put("virtual_router_id", info.virtualRouterId ?: JSONObject.NULL)
        .put("baseline_mode", info.baselineMode)
        .put("baseline_mode_source", info.baselineModeSource)
        .put("hfl_mode", info.hflMode)
        .put("hfl_mode_source", info.hflModeSource)
        .put("num_cycles", info.numCycles)
        .put("num_cycles_source", info.numCyclesSource)
        .put("baseline_measurements", info.plannedMeasurements)
        .put("satisfaction_before", satisfactionBefore ?: JSONObject.NULL)
        .put("satisfaction_baseline_mean", s.mean ?: JSONObject.NULL)
        .put("satisfaction_baseline_n", s.n)
        .put("satisfaction_baseline_samples", org.json.JSONArray().apply { s.samples.forEach { put(it ?: JSONObject.NULL) } })
    central?.keys()?.forEach { k -> row.put(k, central.get(k)) }
    return row
}

