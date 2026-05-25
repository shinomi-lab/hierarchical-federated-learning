package com.example.hfl_experiment.worker

import android.content.Context
import com.example.hfl_experiment.network.NetworkClient
import com.example.hfl_experiment.training.TrainingMetricsStore
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import androidx.work.ListenableWorker.Result as WorkResult
import com.example.hfl_experiment.AppConfig
import com.example.hfl_experiment.util.logging.RealTimeLogger

class UploadMetricsWorker(
    appContext: Context,
    params: WorkerParameters
) : CoroutineWorker(appContext, params) {

    companion object {
        private const val LOG_TAG = "UploadMetricsWorker"
    }

    override suspend fun doWork(): WorkResult {
        try {
            // Early exit if no metrics to send
            val nd = TrainingMetricsStore.listAll()
            if (nd.isNullOrBlank()) {
                RealTimeLogger.i(LOG_TAG, "No metrics to upload; finishing successfully")
                return WorkResult.success()
            }

            val baseUrl = AppConfig.getEdgeBaseUrl(applicationContext)
            val token = AppConfig.getServerAuthToken(applicationContext)
            val client = NetworkClient(context = this.applicationContext, baseUrl = baseUrl, authToken = token)
            try {
                // Parse NDJSON and send each line individually to /upload_training_metrics
                val lines = nd.lines().map { it.trim() }.filter { it.isNotEmpty() }
                if (lines.isEmpty()) {
                    RealTimeLogger.i(LOG_TAG, "No metrics lines after trimming; finishing successfully")
                    return WorkResult.success()
                }

                var allOk = true
                for (l in lines) {
                    try {
                        val jo = org.json.JSONObject(l)
                        // edge_id: prefer terminal_id saved in the record, fallback to app's packageName
                        val edgeId = jo.optString("terminal_id", "")
                        val round = jo.optInt("round", 0)

                        // accuracy/loss may or may not exist in stored metric; default to -1.0 to indicate unknown
                        val accuracy = if (jo.has("accuracy")) jo.optDouble("accuracy", Double.NaN) else Double.NaN
                        val loss = if (jo.has("loss")) jo.optDouble("loss", Double.NaN) else Double.NaN

                        val accToSend = if (accuracy.isNaN()) -1.0 else accuracy
                        val lossToSend = if (loss.isNaN()) -1.0 else loss

                        // event_timestamp: convert timestamp_ms if present
                        var tsIso: String? = null
                        if (jo.has("timestamp_ms")) {
                            try {
                                val tms = jo.optLong("timestamp_ms", -1L)
                                if (tms > 0) {
                                    tsIso = java.time.Instant.ofEpochMilli(tms).toString()
                                }
                            } catch (_: Exception) { /* ignore */ }
                        }

                        // Prefer using the persisted data_id if present so server-side duplicate checks work
                        val persistedDataId = if (jo.has("data_id")) jo.optString("data_id") else null
                        val dataIdToSend = if (persistedDataId.isNullOrBlank()) null else persistedDataId

                        val appType = if (jo.has("app_type")) jo.optString("app_type") else null
                        val appIndex = if (jo.has("app_index")) jo.optInt("app_index") else null
                        val satisfactionBefore = if (jo.has("satisfaction_before")) jo.optDouble("satisfaction_before") else null
                        val satisfactionAfter = if (jo.has("satisfaction_after")) jo.optDouble("satisfaction_after") else null
                        val tpMeasured = if (jo.has("tp_measured_mbps")) jo.optDouble("tp_measured_mbps") else null
                        val rttMeasured = if (jo.has("rtt_measured_ms")) jo.optDouble("rtt_measured_ms") else null

                        val ok = client.sendTrainingMetric(
                            edgeId = if (edgeId.isBlank()) applicationContext.packageName else edgeId,
                            round = round,
                            accuracy = accToSend,
                            loss = lossToSend,
                            dataId = dataIdToSend,
                            eventTimestamp = tsIso,
                            appType = appType,
                            appIndex = appIndex,
                            satisfactionBefore = satisfactionBefore,
                            satisfactionAfter = satisfactionAfter,
                            tpMeasured = tpMeasured,
                            rttMeasured = rttMeasured
                        )

                        if (!ok) {
                            RealTimeLogger.w(LOG_TAG, "Failed to send metric for line: $l")
                            allOk = false
                            break
                        } else {
                            RealTimeLogger.i(LOG_TAG, "Successfully sent metric for line: $l")
                        }
                    } catch (e: Exception) {
                        RealTimeLogger.w(LOG_TAG, "Exception while sending metric line: ${e.message}")
                        allOk = false
                        break
                    }
                }

                if (allOk) {
                    // Clear local store on full success
                    TrainingMetricsStore.clearAll()
                    RealTimeLogger.i(LOG_TAG, "All metrics uploaded successfully; cleared local store")
                    return WorkResult.success()
                } else {
                    RealTimeLogger.w(LOG_TAG, "Not all metrics uploaded; will retry later")
                    return WorkResult.retry()
                }
            } finally {
                client.shutdown()
            }

        } catch (e: Exception) {
            RealTimeLogger.w(LOG_TAG, "Exception in UploadMetricsWorker: ${e.message}")
            // Retry with backoff
            return WorkResult.retry()
        }
    }
}
