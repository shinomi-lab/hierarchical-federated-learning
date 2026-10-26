package com.example.hfl_experiment.util

import android.content.Context
import com.example.hfl_experiment.util.logging.RealTimeLogger
import java.io.File
import java.io.FileOutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

object PerfLogger {
    private const val FILE_NAME = "perf_times.txt"
    @Volatile
    private var initialized = false
    private lateinit var baseDir: File

    fun init(context: Context) {
        if (initialized) return
        synchronized(this) {
            if (initialized) return
            baseDir = context.filesDir
            initialized = true
        }
    }

    fun append(tag: String, msg: String) {
        try {
            val timestamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.US).format(Date())
            val line = "$timestamp [$tag] $msg\n"
            RealTimeLogger.i(tag, msg)
            if (!initialized) return
            val outFile = File(baseDir, FILE_NAME)
            synchronized(this) {
                FileOutputStream(outFile, true).use { it.write(line.toByteArray()) }
            }
        } catch (e: Throwable) {
            // Swallow - logging should not crash app
            RealTimeLogger.w("PerfLogger", "failed to append perf log: ${e.message}")
        }
    }

    fun path(): String? = if (initialized) File(baseDir, FILE_NAME).absolutePath else null
}
