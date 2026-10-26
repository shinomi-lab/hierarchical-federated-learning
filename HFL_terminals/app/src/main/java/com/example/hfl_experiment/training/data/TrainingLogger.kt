package com.example.hfl_experiment.training.data

import android.content.Context
import com.example.hfl_experiment.util.logging.RealTimeLogger
import com.example.hfl_experiment.util.logging.ServerStyleLogger
import java.io.File
import java.io.FileWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * TrainingLogger
 * - 学習に関するイベントをアプリの filesDir または外部指定ディレクトリに .log 形式で記録する
 * - 初期化時にファイルと親ディレクトリを確実に作成する
 *
 * IMPORTANT: If an externalLogDir is provided, this class will REQUIRE that the directory is
 * writable and will throw an IllegalStateException during initialization if it cannot create
 * or write the log file there. This ensures callers are aware immediately if the requested
 * host path cannot be used (useful for forcing writes to e.g. a Windows host path).
 */
class TrainingLogger(private val context: Context, private val externalLogDir: File? = null) {

    private val TAG = "TrainingLogger"
    // If externalLogDir provided, prefer it; otherwise use context.filesDir
    private val selectedLogDir: File = externalLogDir?.takeIf { it.absolutePath.isNotBlank() } ?: context.filesDir
    private val logFile: File = File(selectedLogDir, "training_events.log")
    private val sdf = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.getDefault())

    init {
        try {
            // attempt to create directory
            if (!selectedLogDir.exists()) {
                val created = selectedLogDir.mkdirs()
                if (!created) {
                    val msg = "Failed to create log directory: ${selectedLogDir.absolutePath}"
                    RealTimeLogger.e(TAG, msg)
                    // If externalLogDir was requested, fail hard
                    if (externalLogDir != null) throw IllegalStateException(msg)
                }
            }

            // ensure file exists and write header if new
            if (!logFile.exists()) {
                try {
                    logFile.parentFile?.mkdirs()
                    logFile.createNewFile()
                    FileWriter(logFile, true).use { writer ->
                        writer.append("timestamp,event,round,extra\n")
                    }
                } catch (e: Exception) {
                    val msg = "Failed to create or write log file at: ${logFile.absolutePath} (${e.message})"
                    RealTimeLogger.e(TAG, msg, e)
                    if (externalLogDir != null) throw IllegalStateException(msg, e)
                    // otherwise continue and hope fallback path works
                }
            }

            // quick sanity write to ensure writability
            try {
                FileWriter(logFile, true).use { it.append("") }
            } catch (e: Exception) {
                val msg = "Log file not writable at: ${logFile.absolutePath} (${e.message})"
                RealTimeLogger.e(TAG, msg, e)
                if (externalLogDir != null) throw IllegalStateException(msg, e)
            }

            RealTimeLogger.i(TAG, "TrainingLogger initialized, path=${logFile.absolutePath}")
        } catch (e: IllegalStateException) {
            // rethrow to make failure explicit
            throw e
        } catch (e: Exception) {
            // Generic fallback behavior: if external requested but something else failed, fail hard
            if (externalLogDir != null) {
                val msg = "Unexpected failure initializing TrainingLogger at ${selectedLogDir.absolutePath}: ${e.message}"
                RealTimeLogger.e(TAG, msg, e)
                throw IllegalStateException(msg, e)
            } else {
                RealTimeLogger.e(TAG, "Failed to initialize TrainingLogger at ${selectedLogDir.absolutePath}: ${e.message}", e)
            }
        }
    }

    @Synchronized
    fun logEvent(event: String, roundNumber: Int = -1, extra: String = "") {
        try {
            val timestamp = sdf.format(Date())
            val line = "${escapeCsv(timestamp)},${escapeCsv(event)},${roundNumber},${escapeCsv(extra)}\n"
            FileWriter(logFile, true).use { writer ->
                writer.append(line)
            }
            // Also append a server-style textual log line for easy aggregation on server-like tools
            try { ServerStyleLogger.append("INFO", "TrainingLogger", "event=${event} round=${roundNumber} extra=${extra}") } catch (_: Throwable) {}
        } catch (e: Exception) {
            RealTimeLogger.e(TAG, "Failed to log event: ${e.message}", e)
            // If user explicitly required external path, escalate by throwing so caller notices
            if (externalLogDir != null) throw IllegalStateException("Failed to write to required external log file: ${logFile.absolutePath}", e)
        }
    }

    fun logTrainingStart(roundNumber: Int) {
        logEvent("training_start", roundNumber)
    }

    fun logTrainingComplete(roundNumber: Int) {
        logEvent("training_complete", roundNumber)
    }

    fun logUploadStart(roundNumber: Int) {
        logEvent("upload_start", roundNumber)
    }

    fun logUploadComplete(roundNumber: Int) {
        logEvent("upload_complete", roundNumber)
    }

    fun logNewModelReceived(roundNumber: Int) {
        logEvent("new_model_received", roundNumber)
    }

    fun getLogPath(): String = logFile.absolutePath

    private fun escapeCsv(s: String): String {
        val t = s.replace("\"", "\"\"")
        return if (t.contains(",") || t.contains("\"") || t.contains("\n")) "\"$t\"" else t
    }
}
