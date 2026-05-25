package com.example.hfl_experiment.telemetry

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit
import android.content.Intent
import com.example.hfl_experiment.util.logging.RealTimeLogger

class TelemetryWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {
    companion object {
        private const val TAG = "TelemetryWorker"
        const val UNIQUE_NAME = "hfl_telemetry_worker"
        const val ACTION_TELEMETRY_SENT = "com.example.hfl_experiment.ACTION_TELEMETRY_SENT"

        fun schedulePeriodic(context: Context, intervalMinutes: Long = 60) {
            try {
                val req = PeriodicWorkRequestBuilder<TelemetryWorker>(intervalMinutes, TimeUnit.MINUTES).build()
                WorkManager.getInstance(context).enqueueUniquePeriodicWork(UNIQUE_NAME, ExistingPeriodicWorkPolicy.KEEP, req)
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "schedulePeriodic failed: ${e.message}")
            }
        }
    }

    override suspend fun doWork(): Result {
        try {
            val sent = TelemetrySender.send(applicationContext)

            // Broadcast the result so UI (MainActivity) can show a toast/snackbar
            try {
                val intent = Intent(ACTION_TELEMETRY_SENT)
                intent.putExtra("success", sent)
                applicationContext.sendBroadcast(intent)
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "failed to send telemetry broadcast: ${e.message}")
            }

            if (sent) {
                RealTimeLogger.i(TAG, "Telemetry sent successfully")
                return Result.success()
            } else {
                RealTimeLogger.w(TAG, "Telemetry send failed; will retry")
                return Result.retry()
            }
        } catch (e: Exception) {
            RealTimeLogger.e(TAG, "TelemetryWorker exception: ${e.message}", e)
             // Broadcast failure too
             try {
                 val intent = Intent(ACTION_TELEMETRY_SENT)
                 intent.putExtra("success", false)
                 applicationContext.sendBroadcast(intent)
             } catch (_: Exception) {}
             return Result.retry()
         }
     }
 }
