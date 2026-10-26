package com.example.hfl_experiment.worker

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.example.hfl_experiment.AppConfig
import com.example.hfl_experiment.network.NetworkClient
import com.example.hfl_experiment.training.TrainingMetricsStore
import com.example.hfl_experiment.util.logging.RealTimeLogger
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** 未送信キューのメトリクス行を、中央が保存に成功した行から順に取り除きながら送る。 */
class UploadMetricsWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {

    companion object {
        private const val LOG_TAG = "UploadMetricsWorker"
        const val UNIQUE_WORK_NAME = "upload_metrics"

        /** 記録のたびに呼ぶ。手動の「メトリクス送信」ボタンも同じ関数を使う。 */
        fun enqueue(context: Context) {
            val req = OneTimeWorkRequestBuilder<UploadMetricsWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 10, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(UNIQUE_WORK_NAME, ExistingWorkPolicy.APPEND_OR_REPLACE, req)
        }

        /** キューが空になるまで送る（送信中に追記された行も拾う）。1行でも失敗したら false。 */
        internal suspend fun drainOutbox(send: suspend (JSONObject) -> Boolean): Boolean {
            while (true) {
                val batch = TrainingMetricsStore.pending()
                if (batch.isEmpty()) return true
                for (row in batch) {
                    if (!send(row)) return false
                    TrainingMetricsStore.markSent(row.getString("data_id"))
                }
            }
        }
    }

    override suspend fun doWork(): Result {
        return try {
            TrainingMetricsStore.init(applicationContext)
            val client = NetworkClient(
                context = applicationContext,
                baseUrl = AppConfig.getEdgeBaseUrl(applicationContext),
                authToken = AppConfig.getServerAuthToken(applicationContext)
            )
            if (drainOutbox { client.sendRowJson(it) }) {
                RealTimeLogger.i(LOG_TAG, "all pending metrics delivered")
                Result.success()
            } else {
                RealTimeLogger.w(LOG_TAG, "pending metrics remain (${TrainingMetricsStore.pending().size}); will retry")
                Result.retry()
            }
        } catch (e: Exception) {
            RealTimeLogger.w(LOG_TAG, "UploadMetricsWorker exception: ${e.message}")
            Result.retry()
        }
    }
}
