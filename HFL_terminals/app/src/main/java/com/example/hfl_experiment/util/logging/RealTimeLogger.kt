package com.example.hfl_experiment.util.logging

import android.content.Context
import android.util.Log
import java.io.File
import java.io.FileWriter
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import org.json.JSONObject

object RealTimeLogger {
    private const val TAG = "RealTimeLogger"
    private const val LOG_FILE_NAME = "realtime_events.log"
    private var logFile: File? = null
    private val dateFmt = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US)

    fun init(context: Context) {
        try {
            val dir = File(context.filesDir, "logs")
            if (!dir.exists()) dir.mkdirs()
            logFile = File(dir, LOG_FILE_NAME)
            if (!logFile!!.exists()) logFile!!.createNewFile()
            Log.i(TAG, "init: log file=${logFile!!.absolutePath}")
        } catch (e: Exception) {
            Log.w(TAG, "init failed: ${e.message}")
            logFile = null
        }
    }

    // Start a new session: initialize logger and emit a session_start event
    fun startSession(context: Context) {
        try {
            init(context)
            logEvent("session_start", mapOf("pid" to android.os.Process.myPid(), "pkg" to (context.packageName ?: "")))
        } catch (e: Exception) {
            Log.w(TAG, "startSession failed: ${e.message}")
        }
    }

    // Close session: flush and emit a session_end marker then close underlying file
    fun closeSession() {
        try {
            logEvent("session_end", mapOf("ts" to nowIso()))
        } catch (_: Exception) {}
        try {
            // attempt to sync and close file handle
            try {
                logFile?.let { f ->
                    try {
                        // no persistent FileOutputStream open here; rely on file existing
                    } catch (_: Throwable) {}
                }
            } catch (_: Throwable) {}
        } catch (_: Exception) {}
    }

    private fun nowIso(): String = dateFmt.format(Date())

    private fun appendLine(json: JSONObject) {
        try {
            val f = logFile ?: return
            synchronized(this) {
                FileWriter(f, true).use { w ->
                    w.append(json.toString())
                    w.append('\n')
                    w.flush()
                }
            }
        } catch (e: IOException) {
            Log.w(TAG, "appendLine failed: ${e.message}")
        }
    }

    private fun buildEvent(name: String, payload: Map<String, Any?> = emptyMap()): JSONObject {
        val obj = JSONObject()
        obj.put("schema_version", 1)
        obj.put("ts", nowIso())
        obj.put("ts_ms", System.currentTimeMillis())
        obj.put("event", name)
        // 実験追跡に必須のトップレベルフィールド（サーバーログとの突き合わせ用）
        try {
            val runId = com.example.hfl_experiment.experiment.ExperimentContext.runId
            obj.put("run_id", runId.ifEmpty { "none" })
        } catch (_: Exception) { obj.put("run_id", "none") }
        try {
            val tid = com.example.hfl_experiment.experiment.ExperimentContext.terminalId
            obj.put("terminal_id", tid.ifEmpty { "unknown" })
        } catch (_: Exception) { obj.put("terminal_id", "unknown") }
        try {
            obj.put("round_id", com.example.hfl_experiment.experiment.ExperimentContext.currentRound)
        } catch (_: Exception) {}
        val meta = JSONObject()
        for ((k, v) in payload) {
            try {
                when (v) {
                    null -> meta.put(k, JSONObject.NULL)
                    is Number -> meta.put(k, v)
                    is Boolean -> meta.put(k, v)
                    else -> meta.put(k, v.toString())
                }
            } catch (e: Exception) {
                meta.put(k, "<encode_error>")
            }
        }
        obj.put("meta", meta)
        return obj
    }

    // Generic event
    fun logEvent(name: String, payload: Map<String, Any?> = emptyMap()) {
        try {
            val j = buildEvent(name, payload)
            appendLine(j)
            Log.d(TAG, "event=$name meta=$payload")
        } catch (e: Exception) {
            Log.w(TAG, "logEvent failed: ${e.message}")
        }
    }

    // Convenience helpers
    fun modelDownloadStart(source: String? = null) = logEvent("model_download_start", mapOf("source" to source))
    fun modelDownloadEnd(source: String? = null, sizeBytes: Long? = null, sha256: String? = null) = logEvent("model_download_end", mapOf("source" to source, "size_bytes" to sizeBytes, "sha256" to sha256))

    fun trainingStart(epochs: Int) = logEvent("training_start", mapOf("epochs" to epochs))
    fun trainingEnd(success: Boolean) = logEvent("training_end", mapOf("success" to success))

    fun epochStart(epoch: Int) = logEvent("epoch_start", mapOf("epoch" to epoch))
    fun epochEnd(epoch: Int, valLoss: Double?, valAcc: Double?) = logEvent("epoch_end", mapOf("epoch" to epoch, "val_loss" to valLoss, "val_acc" to valAcc))

    fun validationStart(epoch: Int) = logEvent("validation_start", mapOf("epoch" to epoch))
    fun validationEnd(epoch: Int, valLoss: Double?, valAcc: Double?) = logEvent("validation_end", mapOf("epoch" to epoch, "val_loss" to valLoss, "val_acc" to valAcc))

    fun uploadStart(targetUrl: String?) = logEvent("upload_start", mapOf("url" to targetUrl))
    fun uploadEnd(targetUrl: String?, success: Boolean, durationMs: Long? = null) = logEvent("upload_end", mapOf("url" to targetUrl, "success" to success, "duration_ms" to durationMs))

    fun anomaly(msg: String, details: String? = null) = logEvent("anomaly", mapOf("msg" to msg, "details" to details))

    // Shortcuts to emulate Android Log style (used across codebase)
    fun i(tag: String, msg: String) {
        try {
            Log.i(tag, msg)
            logEvent("info", mapOf("tag" to tag, "msg" to msg))
        } catch (_: Exception) {}
    }

    fun d(tag: String, msg: String) {
        try {
            Log.d(tag, msg)
            logEvent("debug", mapOf("tag" to tag, "msg" to msg))
        } catch (_: Exception) {}
    }

    fun w(tag: String, msg: String) {
        try {
            Log.w(tag, msg)
            logEvent("warn", mapOf("tag" to tag, "msg" to msg))
        } catch (_: Exception) {}
    }

    fun e(tag: String, msg: String, t: Throwable? = null) {
        try {
            if (t != null) Log.e(tag, msg, t) else Log.e(tag, msg)
            val details = t?.let { it::class.java.name + ": " + (it.message ?: "") }
            logEvent("error", mapOf("tag" to tag, "msg" to msg, "details" to details))
        } catch (_: Exception) {}
    }

    // Provide list of log files for telemetry/debugging
    fun getLogFiles(context: Context): List<File> {
        try {
            val files = mutableListOf<File>()
            // include the realtime events log if initialized
            try { logFile?.let { if (it.exists()) files.add(it) } } catch (_: Exception) {}

            // include files in app's log directory (filesDir/log)
            try {
                val dir = File(context.filesDir, "log")
                if (dir.exists() && dir.isDirectory) {
                    dir.listFiles()?.filter { it.isFile }?.forEach { files.add(it) }
                }
            } catch (_: Exception) {}

            return files.distinctBy { it.absolutePath }
        } catch (_: Exception) {
            return emptyList()
        }
    }
}
