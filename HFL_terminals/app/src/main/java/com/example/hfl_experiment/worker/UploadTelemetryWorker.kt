package com.example.hfl_experiment.worker

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import androidx.work.ListenableWorker.Result as WorkResult
import com.example.hfl_experiment.network.NetworkClient
import com.example.hfl_experiment.AppConfig
import com.example.hfl_experiment.util.logging.RealTimeLogger

class UploadTelemetryWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {
    companion object {
        private const val LOG_TAG = "UploadTelemetryWorker"
        const val UNIQUE_WORK_NAME = "hfl_upload_telemetry_worker"
    }

    override suspend fun doWork(): WorkResult {
        try {
            // Expect telemetry JSON string passed in inputData under key "telemetry_json"
            val telemetryJson = inputData.getString("telemetry_json")
            if (telemetryJson.isNullOrBlank()) {
                RealTimeLogger.i(LOG_TAG, "No telemetry payload provided; nothing to do")
                return WorkResult.success()
            }

            val baseUrl = AppConfig.getEdgeBaseUrl(applicationContext)
            val token = AppConfig.getServerAuthToken(applicationContext)
            val client = NetworkClient(context = this.applicationContext, baseUrl = baseUrl, authToken = token)

            val ok = client.sendTelemetry(applicationContext.packageName, telemetryJson)
            if (ok) {
                RealTimeLogger.i(LOG_TAG, "Telemetry uploaded successfully")
                return WorkResult.success()
            } else {
                RealTimeLogger.w(LOG_TAG, "Telemetry upload failed; will retry")
                return WorkResult.retry()
            }
        } catch (e: Exception) {
            RealTimeLogger.w(LOG_TAG, "Exception in UploadTelemetryWorker: ${e.message}")
            return WorkResult.retry()
        }
    }
}
