package com.example.hfl_experiment.work

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import androidx.work.Constraints
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.ExistingWorkPolicy
import androidx.work.Data
import com.example.hfl_experiment.network.NetworkClient
import com.example.hfl_experiment.network.UploadClientLogsHttpException
import java.io.File
import com.example.hfl_experiment.AppConfig
import com.example.hfl_experiment.experiment.ExperimentContext
import com.example.hfl_experiment.util.logging.RealTimeLogger
import org.json.JSONObject

/**
 * WorkManager CoroutineWorker that uploads client log files under filesDir/log to server using NetworkClient.uploadClientLogs.
 * On success, moves uploaded files to filesDir/log/sent/ to avoid reupload.
 */
class UploadLogsWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {
    companion object {
        private const val TAG = "UploadLogsWorker"
        const val UNIQUE_WORK_NAME = "hfl_upload_logs_worker"
        /** 学習中止後などに送るエラーレポート用（直前のスナップショットのみアップロードし、学習中 CSV は移動しない） */
        const val UNIQUE_ERROR_REPORT_WORK_NAME = "hfl_upload_error_report_worker"
        const val KEY_SNAPSHOTS_ONLY = "snapshots_only"

        /** `filesDir/log` にコピーしてからアップロードする固定名（成功後のみ `sent/` へ移動） */
        const val ERROR_REPORT_REALTIME_NAME = "error_report_realtime_events.log"
        const val ERROR_REPORT_MODEL_NAME = "error_report_model_update_log.txt"
        const val ERROR_REPORT_TRAINING_NAME = "error_report_training_log.csv"

        fun enqueueOnce(context: Context) {
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()
            val req = OneTimeWorkRequestBuilder<UploadLogsWorker>()
                .setConstraints(constraints)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(UNIQUE_WORK_NAME, ExistingWorkPolicy.KEEP, req)
        }

        fun enqueueErrorReportOnce(context: Context) {
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()
            val data = Data.Builder().putBoolean(KEY_SNAPSHOTS_ONLY, true).build()
            val req = OneTimeWorkRequestBuilder<UploadLogsWorker>()
                .setConstraints(constraints)
                .setInputData(data)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(
                UNIQUE_ERROR_REPORT_WORK_NAME,
                ExistingWorkPolicy.REPLACE,
                req
            )
        }
    }

    override suspend fun doWork(): Result {
        val ctx = applicationContext
        try {
            val logDir = File(ctx.filesDir, "log")
            if (!logDir.exists() || !logDir.isDirectory) {
                RealTimeLogger.i(TAG, "no log dir found, nothing to upload")
                return Result.success()
            }
            val snapshotsOnly = inputData.getBoolean(KEY_SNAPSHOTS_ONLY, false)
            val files = if (snapshotsOnly) {
                listOfNotNull(
                    File(logDir, ERROR_REPORT_REALTIME_NAME).takeIf { it.isFile && it.length() > 0L },
                    File(logDir, ERROR_REPORT_MODEL_NAME).takeIf { it.isFile && it.length() > 0L },
                    File(logDir, ERROR_REPORT_TRAINING_NAME).takeIf { it.isFile && it.length() > 0L },
                )
            } else {
                logDir.listFiles()?.filter { f ->
                    f.isFile && f.length() > 0L && f.parentFile?.canonicalPath == logDir.canonicalPath &&
                        !f.name.startsWith("error_report_")
                } ?: emptyList()
            }

            val baseUrl = AppConfig.getEdgeBaseUrl(ctx)
            val token = AppConfig.getServerAuthToken(ctx)
            val client = NetworkClient(ctx, baseUrl, authToken = token)
            val tid = AppConfig.getTerminalId(ctx)
            val metaJson = try {
                val meta = client.fetchMeta(forceRefresh = true)
                val jo = JSONObject()
                jo.put("terminal_id", tid)
                jo.put("round", meta.round)
                jo.put("timestamp_ms", System.currentTimeMillis())
                jo.put("report_kind", if (snapshotsOnly) "error_report" else "bulk_logs")
                val rid = ExperimentContext.runId
                if (rid.isNotBlank()) jo.put("run_id", rid)
                jo.toString()
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "UploadLogsWorker: fetchMeta failed: ${e.message} - sending minimal meta without round")
                val jo = JSONObject()
                jo.put("terminal_id", tid)
                jo.put("timestamp_ms", System.currentTimeMillis())
                jo.put("report_kind", if (snapshotsOnly) "error_report" else "bulk_logs")
                val rid = ExperimentContext.runId
                if (rid.isNotBlank()) jo.put("run_id", rid)
                jo.toString()
            }

            if (files.isEmpty()) {
                if (!snapshotsOnly) {
                    RealTimeLogger.i(TAG, "no log files found, nothing to upload")
                    return Result.success()
                }
                RealTimeLogger.i(TAG, "error report: no snapshot files — sending meta-only to edge (server accepts report_kind=error_report)")
                val resp = try {
                    client.uploadClientLogs(tid, emptyMap(), metaJson)
                } catch (e: UploadClientLogsHttpException) {
                    RealTimeLogger.w(TAG, "uploadClientLogs HTTP ${e.httpCode} (meta-only): ${e.message}")
                    if (e.httpCode in 400..499) return Result.success()
                    return Result.retry()
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "uploadClientLogs failed: ${e.message}")
                    return Result.retry()
                }
                RealTimeLogger.i(TAG, "uploadClientLogs response (meta-only): ${resp.take(200)}")
                return Result.success()
            }

            val parts = mutableMapOf<String, File>()
            files.forEachIndexed { i, f -> parts["file_$i"] = f }

            RealTimeLogger.i(TAG, "attempting upload of ${parts.size} files")
            val resp = try {
                client.uploadClientLogs(tid, parts, metaJson)
            } catch (e: UploadClientLogsHttpException) {
                RealTimeLogger.w(
                    TAG,
                    "uploadClientLogs HTTP ${e.httpCode} (no retry for 4xx): ${e.message}"
                )
                // 4xx は設定・ペイロード起因でリトライしても改善しないことが多い
                if (e.httpCode in 400..499) return Result.success()
                return Result.retry()
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "uploadClientLogs failed: ${e.message}")
                return Result.retry()
            }


            RealTimeLogger.i(TAG, "uploadClientLogs response: ${resp.take(200)}")

            // On success, move uploaded files to sent/ (bulk mode moves everything in log/; error report only moves snapshot copies)
            try {
                val sentDir = File(logDir, "sent")
                if (!sentDir.exists()) sentDir.mkdirs()
                val stamp = System.currentTimeMillis()
                files.forEach { f ->
                    try {
                        val dest = File(sentDir, "${stamp}_${f.name}")
                        f.renameTo(dest)
                    } catch (e: Exception) {
                        RealTimeLogger.w(TAG, "failed to move file ${f.name} to sent: ${e.message}")
                    }
                }
            } catch (e: Exception) {

                RealTimeLogger.w(TAG, "post-upload file move failed: ${e.message}")
            }

            return Result.success()
        } catch (e: Exception) {

            RealTimeLogger.e(TAG, "UploadLogsWorker exception: ${e.message}", e)
            return Result.retry()
        }
    }
}
