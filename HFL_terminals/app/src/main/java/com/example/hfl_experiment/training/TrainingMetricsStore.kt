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
    private const val FILE_NAME = "training_metrics.jsonl"
    private lateinit var baseDir: File
    private var initialized = false

    fun init(context: Context) {
        if (initialized) return
        baseDir = context.filesDir
        initialized = true
    }

    fun record(metric: TrainingMetric) {
        try {
            if (!initialized) return
            val jo = metric.toJson()
            if (!jo.has("data_id")) {
                jo.put("data_id", java.util.UUID.randomUUID().toString())
            }
            val jsonLine = jo.toString() + "\n"
            synchronized(this) {
                // 共通ファイルへ追記（後方互換）
                FileOutputStream(File(baseDir, FILE_NAME), true).use { it.write(jsonLine.toByteArray()) }
                // run_id ごとの専用ファイルへも書き込む（消失リスク分散）
                val runId = jo.optString("run_id", "")
                if (runId.isNotEmpty()) {
                    val perRunDir = File(baseDir, "metrics_by_run")
                    perRunDir.mkdirs()
                    FileOutputStream(File(perRunDir, "metrics_${runId}.jsonl"), true).use { it.write(jsonLine.toByteArray()) }
                }
            }
            PerfLogger.append("TrainingMetrics", "recorded metric ts=${metric.timestamp} run_id=${metric.runId} cycle=${metric.cycleNumber} data_id=${jo.optString("data_id")}")
        } catch (e: Throwable) {
            PerfLogger.append("TrainingMetrics", "failed to record metric: ${e.message}")
        }
    }

    /**
     * アップロード成功後にのみ呼ぶ。
     * run_id 指定時は共通ファイルは残し、per-run ファイルのみ削除する。
     * run_id 未指定の場合は従来通り共通ファイルを削除する。
     */
    fun clearAll(runId: String? = null) {
        try {
            if (!initialized) return
            if (!runId.isNullOrBlank()) {
                val f = File(File(baseDir, "metrics_by_run"), "metrics_${runId}.jsonl")
                if (f.exists()) {
                    f.delete()
                    PerfLogger.append("TrainingMetrics", "cleared per-run metrics run_id=$runId")
                }
            } else {
                val f = File(baseDir, FILE_NAME)
                if (f.exists()) {
                    f.delete()
                    PerfLogger.append("TrainingMetrics", "cleared all metrics")
                }
            }
        } catch (e: Throwable) {
            PerfLogger.append("TrainingMetrics", "failed to clear metrics: ${e.message}")
        }
    }

    fun listAll(): String? {
        if (!initialized) return null
        val f = File(baseDir, FILE_NAME)
        return if (f.exists()) f.readText() else null
    }

    /**
     * 直近の JSONL レコードの satisfaction_after を更新する。
     * measureSatisfactionAfter() が呼ばれた際に、既存レコードへ遡及的に値を書き込む。
     */
    fun updateLastSatisfactionAfter(value: Double) {
        try {
            if (!initialized) return
            synchronized(this) {
                val f = File(baseDir, FILE_NAME)
                if (!f.exists()) return
                val lines = f.readLines().toMutableList()
                if (lines.isEmpty()) return
                // 最後の行を更新
                val lastIdx = lines.lastIndex
                val lastLine = lines[lastIdx]
                if (lastLine.isBlank()) return
                val jo = JSONObject(lastLine)
                jo.put("satisfaction_after", value)
                lines[lastIdx] = jo.toString()
                f.writeText(lines.joinToString("\n") + "\n")
            }
        } catch (e: Throwable) {
            PerfLogger.append("TrainingMetrics", "failed to update satisfaction_after: ${e.message}")
        }
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
        val cycleNumber: Int? = null,       // 5サイクル中の何サイクル目か
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
        val rttMeasured: Double? = null
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
            return jo
        }
    }
}
