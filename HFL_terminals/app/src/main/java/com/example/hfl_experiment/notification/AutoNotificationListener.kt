package com.example.hfl_experiment.notification

import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import com.example.hfl_experiment.util.logging.RealTimeLogger

/**
 * AutoNotificationListener: listens for notifications posted by the app and automatically
 * triggers the pending intent action when a switch suggestion notification is seen.
 * Note: requires the user to grant Notification Access for this app.
 */
class AutoNotificationListener : NotificationListenerService() {
    private val TAG = "AutoNotificationListener"

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        super.onNotificationPosted(sbn)
        try {
            val pkg = sbn.packageName ?: return
            if (pkg != this.packageName) return
            val notif = sbn.notification
            val actions = notif.actions ?: return
            for (act in actions) {
                // attempt to fire the action's PendingIntent (requires appropriate flags)
                try {
                    val pi = act.actionIntent
                    pi?.send()
                    RealTimeLogger.i(TAG, "AutoNotificationListener fired action for ${sbn.id}")
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "Failed to send pending intent: ${e.message}")
                }
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "onNotificationPosted error: ${e.message}")
        }
    }

    override fun onListenerConnected() {
        RealTimeLogger.i(TAG, "Notification listener connected")
    }

    override fun onListenerDisconnected() {
        RealTimeLogger.i(TAG, "Notification listener disconnected")
    }
}
