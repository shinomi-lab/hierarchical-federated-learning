package com.example.hfl_experiment.training

import android.content.Context
import java.io.File
import java.io.FileOutputStream

/**
 * Very small CSV logger for training progress. Appends to filesDir/log/training_log.csv
 * Columns: timestamp_ms,type,epoch,progress,val_loss,val_acc,msg
 */
object TrainingSimpleLogger {
    private const val FILE_NAME = "training_log.csv"
    private lateinit var baseDir: File
    private var initialized = false

    fun init(context: Context) {
        if (initialized) return
        // Use a subdirectory "log" under filesDir to make logs easy to find
        baseDir = File(context.filesDir, "log")
        if (!baseDir.exists()) baseDir.mkdirs()
        initialized = true
        ensureHeader()
    }

    private fun ensureHeader() {
        try {
            val f = File(baseDir, FILE_NAME)
            if (!f.exists()) {
                FileOutputStream(f, true).use { out ->
                    out.write("timestamp_ms,type,epoch,progress,val_loss,val_acc,msg\n".toByteArray())
                }
            }
        } catch (_: Throwable) { /* best-effort */ }
    }

    fun appendEpoch(epoch: Int, valLoss: Double?, valAcc: Double?, msg: String? = null) {
        try {
            if (!initialized) return
            val f = File(baseDir, FILE_NAME)
            val ts = System.currentTimeMillis()
            val line = StringBuilder()
            line.append(ts).append(',')
            line.append("epoch").append(',')
            line.append(epoch).append(',')
            line.append("").append(',') // progress empty for epoch rows
            line.append(valLoss?.toString() ?: "").append(',')
            line.append(valAcc?.toString() ?: "").append(',')
            line.append(msg?.replace('\n',' ') ?: "").append('\n')
            synchronized(this) {
                FileOutputStream(f, true).use { it.write(line.toString().toByteArray()) }
            }
        } catch (_: Throwable) { /* ignore */ }
    }

    fun appendProgress(progressFraction: Float, epochApprox: Int? = null, msg: String? = null) {
        try {
            if (!initialized) return
            val f = File(baseDir, FILE_NAME)
            val ts = System.currentTimeMillis()
            val line = StringBuilder()
            line.append(ts).append(',')
            line.append("progress").append(',')
            line.append(epochApprox?.toString() ?: "").append(',')
            line.append(progressFraction).append(',')
            line.append("").append(',') // val_loss empty
            line.append("").append(',') // val_acc empty
            line.append(msg?.replace('\n',' ') ?: "").append('\n')
            synchronized(this) {
                FileOutputStream(f, true).use { it.write(line.toString().toByteArray()) }
            }
        } catch (_: Throwable) { /* ignore */ }
    }

    fun readAll(): String? {
        return if (initialized) File(baseDir, FILE_NAME).takeIf { it.exists() }?.readText() else null
    }
}
