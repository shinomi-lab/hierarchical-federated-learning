package com.example.hfl_experiment.debug

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.example.hfl_experiment.util.logging.RealTimeLogger
import java.io.File

class DumpUploadLogsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        try {
            val f = File(context.filesDir, "upload_logs.csv")
            if (f.exists()) {
                val txt = f.readText()
                RealTimeLogger.i("DumpUploadLogsReceiver", "upload_logs.csv contents:\n$txt")
            } else {
                RealTimeLogger.w("DumpUploadLogsReceiver", "upload_logs.csv not found at ${f.absolutePath}")
            }
        } catch (e: Exception) {
            RealTimeLogger.w("DumpUploadLogsReceiver", "failed to dump upload logs: ${e.message}")
        }
    }
}
