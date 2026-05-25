package com.example.hfl_experiment

import android.app.Activity
import android.app.Application
import android.os.Bundle
import com.example.hfl_experiment.experiment.ExperimentContext
import com.example.hfl_experiment.util.logging.RealTimeLogger
import com.example.hfl_experiment.util.logging.ServerStyleLogger

class HflApplication : Application(), Application.ActivityLifecycleCallbacks {

    private var startedActivities = 0
    override fun onCreate() {
        super.onCreate()

        // 1. ExperimentContext を初期化（terminal_id / experiment_group を確定）
        try {
            AppConfig.loadDeviceConfig(this)
            AppConfig.migrateEdgeSsidPortRewriteFlagIfNeeded(this)
            ExperimentContext.initFromAppConfig(this)
        } catch (e: Exception) {
            android.util.Log.w("HflApplication", "ExperimentContext init failed: ${e.message}")
        }

        // 2. ActivityLifecycleCallbacks を登録（App.kt の機能を統合）
        registerActivityLifecycleCallbacks(this)

        // 3. リアルタイムロガーのセッションを開始
        // これにより、アプリ起動直後から sessions/session_xxxx.log が生成されます
        RealTimeLogger.startSession(this)
        try {
            RealTimeLogger.i("AppConfig", "edge_base_url=${AppConfig.getEdgeBaseUrl(this)}")
        } catch (_: Exception) { }

        // Ensure ServerStyleLogger is initialized at app launch so its file exists immediately
        try {
            ServerStyleLogger.init(this)
            // write an initial marker to the server-style log
            try { ServerStyleLogger.append("INFO", "HflApplication", "ServerStyleLogger initialized at app start") } catch (_: Throwable) {}
        } catch (e: Exception) {
            // best-effort, don't fail startup
            RealTimeLogger.w("HflApplication", "ServerStyleLogger.init failed: ${e.message}")
        }

        // 2. 起動ログを記録
        RealTimeLogger.i("App", "Application onCreate: HFL Experiment App Started")

        // Schedule periodic telemetry worker (default 60 minutes)
        try {
            com.example.hfl_experiment.telemetry.TelemetryWorker.schedulePeriodic(this)
            RealTimeLogger.i("App", "TelemetryWorker scheduled at startup")
        } catch (e: Exception) {
            RealTimeLogger.w("App", "Failed to schedule TelemetryWorker: ${e.message}")
        }

        // Ensure common log files exist so upload logic can always find a candidate
        try {
            val logDir = java.io.File(filesDir, "log")
            if (!logDir.exists()) logDir.mkdirs()

            // training_log.csv (header)
            val trainingCsv = java.io.File(logDir, "training_log.csv")
            if (!trainingCsv.exists()) {
                trainingCsv.createNewFile()
                try {
                    trainingCsv.appendText("timestamp_ms,epoch,val_loss,val_acc\n")
                } catch (_: Throwable) {}
            }

            // model_update_log.txt (placeholder for download attempts etc.)
            val modelUpdateLog = java.io.File(filesDir, "model_update_log.txt")
            if (!modelUpdateLog.exists()) {
                modelUpdateLog.createNewFile()
                try {
                    modelUpdateLog.appendText("${java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss,SSS").format(java.util.Date())} - INFO - HflApplication - Created placeholder model_update_log.txt on app start\n")
                } catch (_: Throwable) {}
            }
        } catch (e: Exception) {
            RealTimeLogger.w("HflApplication", "Failed to create startup log files: ${e.message}")
        }

        // クラッシュ時にフルスタックトレースをファイルへ書き出す
        val defaultHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            try {
                // RealTimeLogger へ記録
                RealTimeLogger.e("HflApplication", "Crash detected on thread=${thread.name}", throwable)

                // crash_<timestamp>.json として独立ファイルに保存（上書きされない）
                val ts = System.currentTimeMillis()
                val crashFile = java.io.File(filesDir, "crash_${ts}.json")
                val jo = org.json.JSONObject()
                jo.put("timestamp_ms", ts)
                jo.put("run_id", ExperimentContext.runId)
                jo.put("terminal_id", ExperimentContext.terminalId)
                jo.put("experiment_group", ExperimentContext.experimentGroup)
                jo.put("current_round", ExperimentContext.currentRound)
                jo.put("thread", thread.name)
                jo.put("exception_class", throwable::class.java.name)
                jo.put("message", throwable.message ?: "")
                // フルスタックトレース（クラッシュ原因行の特定に必須）
                jo.put("stack_trace", throwable.stackTraceToString())
                throwable.cause?.let { cause ->
                    val cjo = org.json.JSONObject()
                    cjo.put("exception_class", cause::class.java.name)
                    cjo.put("message", cause.message ?: "")
                    cjo.put("stack_trace", cause.stackTraceToString())
                    jo.put("cause", cjo)
                }
                jo.put("android_sdk", android.os.Build.VERSION.SDK_INT)
                jo.put("device_model", android.os.Build.MODEL)
                crashFile.writeText(jo.toString(2))
                RealTimeLogger.i("HflApplication", "Crash written to ${crashFile.absolutePath}")
            } catch (_: Throwable) {
                // クラッシュハンドラ自体が落ちてもデフォルト処理を妨げない
            }
            RealTimeLogger.closeSession()
            defaultHandler?.uncaughtException(thread, throwable)
        }
    }

    override fun onTerminate() {
        super.onTerminate()
        RealTimeLogger.closeSession()
    }

    // ActivityLifecycleCallbacks（ServerStyleLogger のフラッシュ管理）
    override fun onActivityCreated(activity: Activity, savedInstanceState: Bundle?) {}
    override fun onActivityDestroyed(activity: Activity) {}
    override fun onActivityPaused(activity: Activity) {}
    override fun onActivityResumed(activity: Activity) {}
    override fun onActivitySaveInstanceState(activity: Activity, outState: Bundle) {}

    override fun onActivityStarted(activity: Activity) {
        startedActivities += 1
        if (startedActivities == 1) {
            RealTimeLogger.i("HflApplication", "App moved to foreground")
        }
    }

    override fun onActivityStopped(activity: Activity) {
        startedActivities = (startedActivities - 1).coerceAtLeast(0)
        if (startedActivities == 0) {
            try {
                RealTimeLogger.i("HflApplication", "App moved to background: flushing server-style logs")
                ServerStyleLogger.flush()
            } catch (e: Exception) {
                RealTimeLogger.w("HflApplication", "Failed to flush ServerStyleLogger on background: ${e.message}")
            }
        }
    }
}
