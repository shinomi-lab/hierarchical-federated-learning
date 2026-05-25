package com.example.hfl_experiment.telemetry

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.tracing.Trace
import com.example.hfl_experiment.util.logging.RealTimeLogger

/**
 * 外部からの時刻同期ブロードキャストを受信し、Perfettoトレースにマーカーを刻印する。
 *
 * Action: com.example.hfl_experiment.SYNC_START
 *
 * Python conductor スクリプトから以下のコマンドで送信される:
 *   adb shell am broadcast -a com.example.hfl_experiment.SYNC_START
 */
class SyncStartReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        // Perfettoトレースに同期マーカーを刻印
        HflTracer.markSyncStart()
        RealTimeLogger.i("SyncStartReceiver", "SYNC_START received — marker stamped at ${System.currentTimeMillis()}")
    }
}
