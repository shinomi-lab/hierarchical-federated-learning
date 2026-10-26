package com.example.hfl_experiment.util.logging

import android.content.Context
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Simple helper to write server-style log lines to filesDir/log/<startupTimestamp>_log.txt
 * Line format: 2025-12-08 14:20:11,031 - INFO - Tag - message
 */
object ServerStyleLogger {
    private var initialized = false
    private lateinit var logFile: File
    private var outStream: java.io.FileOutputStream? = null
    private val fileNameTimestampFormat = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US)
    private val lineTimestampFormat = SimpleDateFormat("yyyy-MM-dd HH:mm:ss,SSS", Locale.US)

    fun init(context: Context) {
        if (initialized) return
        try {
            val dir = File(context.filesDir, "log")
            if (!dir.exists()) dir.mkdirs()
            val ts = fileNameTimestampFormat.format(Date())
            logFile = File(dir, "${ts}_log.txt")
            if (!logFile.exists()) logFile.createNewFile()
            try {
                outStream = java.io.FileOutputStream(logFile, true)
            } catch (_: Throwable) { outStream = null }
            initialized = true
        } catch (_: Throwable) {
            // best-effort, don't throw
        }
    }

    fun getCurrentLogFile(): File? {
        return if (initialized) logFile else null
    }

    fun append(level: String, tag: String, message: String) {
        try {
            if (!initialized) return
            val ts = lineTimestampFormat.format(Date())
            val line = "$ts - ${level.uppercase()} - ${tag} - ${message}\n"
            try {
                val os = outStream ?: java.io.FileOutputStream(logFile, true)
                val bytes = line.toByteArray()
                os.write(bytes)
                os.fd.sync()
                // if we created a transient stream, close it
                if (os !== outStream) try { os.close() } catch (_: Throwable) {}
            } catch (_: Throwable) {
                // swallow
            }
        } catch (_: Throwable) {
            // swallow
        }
    }

    fun flush() {
        try {
            outStream?.fd?.sync()
        } catch (_: Throwable) {}
    }

    fun close() {
        try {
            outStream?.fd?.sync()
        } catch (_: Throwable) {}
        try { outStream?.close() } catch (_: Throwable) {}
        outStream = null
        initialized = false
    }
}
