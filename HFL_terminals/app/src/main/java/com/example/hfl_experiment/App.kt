package com.example.hfl_experiment

import android.app.Activity
import android.app.Application
import android.os.Bundle
import com.example.hfl_experiment.util.logging.RealTimeLogger
import com.example.hfl_experiment.util.logging.ServerStyleLogger

/**
 * Application class that ensures server-style logger is flushed when app goes to background
 * and closed on process termination (best-effort).
 */
class App : Application(), Application.ActivityLifecycleCallbacks {
    private val TAG = "App"
    private var startedActivities = 0

    override fun onCreate() {
        super.onCreate()
        try { ServerStyleLogger.init(this) } catch (_: Throwable) {}
        registerActivityLifecycleCallbacks(this)
    }

    override fun onActivityCreated(activity: Activity, savedInstanceState: Bundle?) {}
    override fun onActivityDestroyed(activity: Activity) {}
    override fun onActivityPaused(activity: Activity) {}
    override fun onActivityResumed(activity: Activity) {}

    override fun onActivityStarted(activity: Activity) {
        startedActivities += 1
        if (startedActivities == 1) {
            // app entered foreground
            RealTimeLogger.i(TAG, "App moved to foreground")
        }
    }

    override fun onActivityStopped(activity: Activity) {
        startedActivities = (startedActivities - 1).coerceAtLeast(0)
        if (startedActivities == 0) {
            // app moved to background
            try {
                RealTimeLogger.i(TAG, "App moved to background: flushing server-style logs")
                ServerStyleLogger.flush()
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "Failed to flush ServerStyleLogger on background: ${e.message}")
            }
        }
    }

    override fun onActivitySaveInstanceState(activity: Activity, outState: Bundle) {}

    override fun onTerminate() {
        try {
            RealTimeLogger.i(TAG, "onTerminate: closing server-style logs")
            ServerStyleLogger.flush()
            ServerStyleLogger.close()
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "Failed to close ServerStyleLogger onTerminate: ${e.message}")
        }
        super.onTerminate()
    }
}
