package com.example.hfl_experiment.notification

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat

object NotificationHelper {
    private const val CHANNEL_ID = "hfl_switch_channel"
    private const val CHANNEL_NAME = "HFL Switch"
    // Must match AndroidManifest action for ConnectReceiver
    const val CONNECT_ACTION = "com.example.hfl_experiment.notification.CONNECT"

    fun ensureChannel(context: Context) {
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            if (nm.getNotificationChannel(CHANNEL_ID) == null) {
                val ch = NotificationChannel(CHANNEL_ID, CHANNEL_NAME, NotificationManager.IMPORTANCE_DEFAULT)
                ch.description = "Notifications suggesting AP switch"
                nm.createNotificationChannel(ch)
            }
        }
    }

    fun showSwitchSuggestion(context: Context, apId: String, ssid: String, notifId: Int = 1001) {
        ensureChannel(context)
        // Build a broadcast intent so NotificationListener or user action will trigger ConnectReceiver
        val intent = Intent(CONNECT_ACTION).apply {
            putExtra("ap_id", apId)
            putExtra("ap_ssid", ssid)
        }
        val pi = PendingIntent.getBroadcast(context, notifId, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

        val builder = NotificationCompat.Builder(context, CHANNEL_ID)
            .setContentTitle("AP 切替の提案")
            .setContentText("接続先を $ssid に切り替えることを検討します")
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setAutoCancel(true)
            .addAction(0, "接続", pi)
            .setOnlyAlertOnce(true)

        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify(notifId, builder.build())
    }
}
