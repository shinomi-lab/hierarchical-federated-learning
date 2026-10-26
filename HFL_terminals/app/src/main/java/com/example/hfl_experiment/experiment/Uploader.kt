package com.example.hfl_experiment.experiment

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.util.concurrent.ConcurrentLinkedQueue

data class DecisionLog(
    val timestamp: Long,
    val deviceIdHash: String,
    val currentAP: String?,
    val chosenAP: String,
    val rawScores: List<Float>,
    val emaScores: List<Float>,
    val tp_mbps: Float,
    val rtt_ms: Float,
    val appType: String,
    val actionResult: String,
    val errorMsg: String?
)

class Uploader(private val context: Context) {
    private val TAG = "Uploader"
    private val q = ConcurrentLinkedQueue<DecisionLog>()
    private val logsDir: File = context.filesDir
    private val csvFile = File(logsDir, "logs/decision_log.csv")

    fun enqueue(log: DecisionLog) {
        q.add(log)
        appendLocalCsv(log)
    }

    private fun appendLocalCsv(l: DecisionLog) {
        try {
            csvFile.parentFile?.mkdirs()
            if (!csvFile.exists()) {
                csvFile.writeText("timestamp,deviceIdHash,currentAP,chosenAP,rawScores,emaScores,tp_mbps,rtt_ms,appType,actionResult,errorMsg\n")
            }
            val raw = l.rawScores.joinToString("|")
            val ema = l.emaScores.joinToString("|")
            val line = listOf(l.timestamp, l.deviceIdHash, l.currentAP ?: "", l.chosenAP, raw, ema, l.tp_mbps, l.rtt_ms, l.appType, l.actionResult, l.errorMsg ?: "").joinToString(",") + "\n"
            csvFile.appendText(line)
        } catch (e: Exception) {
            Log.w(TAG, "appendLocalCsv failed: ${e.message}")
        }
    }

    suspend fun flushNow() = withContext(Dispatchers.IO) {
        // stub: attempt to send queued logs to server; here just clear the queue
        try {
            while (q.isNotEmpty()) {
                val l = q.poll()
                // in real use send over network
            }
            Log.i(TAG, "flushNow completed (stub)")
        } catch (e: Exception) {
            Log.w(TAG, "flushNow failed: ${e.message}")
        }
    }
}
