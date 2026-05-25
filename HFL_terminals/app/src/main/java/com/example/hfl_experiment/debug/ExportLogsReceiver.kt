package com.example.hfl_experiment.debug

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.example.hfl_experiment.util.logging.RealTimeLogger
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlin.coroutines.cancellation.CancellationException

class ExportLogsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == "com.example.hfl_experiment.EXPORT_LOGS") {
            RealTimeLogger.i("ExportLogsReceiver", "Export logs requested via broadcast")

            // BroadcastReceiver内で少し重い処理（ファイルコピー）をするため、goAsync()を使用
            val pendingResult = goAsync()
            CoroutineScope(Dispatchers.IO).launch {
                try {
                    exportLogs(context)
                } catch (e: CancellationException) {
                    RealTimeLogger.i("ExportLogsReceiver", "Log export cancelled")
                }
                finally {
                    // 処理完了をシステムに通知
                    pendingResult.finish()
                }
            }
        }
    }

    private fun exportLogs(context: Context) {
        try {
            // アプリ内ログフォルダ (filesDir/log)
            val srcDir = File(context.filesDir, "log")

            // 出力先: 外部ストレージの shared_logs フォルダ（PCから見えやすい場所）
            val destBase = context.getExternalFilesDir("shared_logs") ?: context.filesDir
            if (!destBase.exists()) destBase.mkdirs()

            val copied = mutableListOf<String>()

            // logフォルダ内の全ファイルをコピー
            if (srcDir.exists() && srcDir.isDirectory) {
                srcDir.listFiles()?.forEach { f ->
                    try {
                        val dest = File(destBase, f.name)
                        f.copyTo(dest, overwrite = true)
                        copied.add(dest.absolutePath)
                    } catch (e: Exception) {
                        RealTimeLogger.w("ExportLogsReceiver", "Failed to copy ${f.name}: ${e.message}")
                    }
                }
            }

            // ルートにある training_metrics 系ファイルもコピー（もしあれば）
            val extraFiles = listOf("training_log.csv", "training_summary.json")
            for (fn in extraFiles) {
                val f = File(context.filesDir, fn)
                if (f.exists()) {
                    try {
                        val dest = File(destBase, f.name)
                        f.copyTo(dest, overwrite = true)
                        copied.add(dest.absolutePath)
                    } catch (e: Exception) {
                        RealTimeLogger.w("ExportLogsReceiver", "Failed to copy extra file ${f.name}: ${e.message}")
                    }
                }
            }

            RealTimeLogger.i("ExportLogsReceiver", "Exported ${copied.size} files to ${destBase.absolutePath}")
        } catch (e: Exception) {
            RealTimeLogger.e("ExportLogsReceiver", "Failed to export logs: ${e.message}", e)
        }
    }
}
