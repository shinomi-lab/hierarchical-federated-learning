package com.example.hfl_experiment.debug

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.example.hfl_experiment.util.logging.RealTimeLogger

/**
 * ADB経由でterminal_idとedge_urlを注入するレシーバー。
 *
 * 使い方:
 *   adb shell am broadcast -a com.example.hfl_experiment.SET_CONFIG \
 *     --es terminal_id "terminal-01" \
 *     --es edge_url "http://192.168.11.6:8001" \
 *     --ez demo_mode true \
 *     --es baseline_mode none   # none / fixed / random / greedy / central / hfl_rec / fixed_ap1（省略可）
 *     --es central_url "http://192.168.11.2:8000"   # central モードの問い合わせ先（省略可。空文字で解除）
 *     --es hfl_mode sync        # sync / async（省略可。エッジの /meta が hfl_mode を返すときはそちらが優先）
 *     --ez clear_manual_config true  # 手動設定の印を外し、その場で IP からの自動設定を試す（SP3d）
 *     --ei num_cycles 20        # 1 セッションのサイクル数 1〜100（省略可。/meta が num_cycles を返すときはそちらが優先）
 *
 * demo_mode は省略可。渡されたときのみ保存し、端末画面に DEMO バッジを表示する（研究運用は既定 false）。
 */
class ConfigInjectionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val terminalId = intent?.getStringExtra("terminal_id")?.trim()
        val edgeUrl = intent?.getStringExtra("edge_url")?.trim()
        val hasDemoMode = intent?.hasExtra("demo_mode") == true
        val baselineMode = intent?.getStringExtra("baseline_mode")?.trim()

        if (!baselineMode.isNullOrEmpty()) {
            try {
                com.example.hfl_experiment.AppConfig.setBaselineMode(context, baselineMode)
                RealTimeLogger.i("ConfigInjection", "Injected: baseline_mode=$baselineMode")
            } catch (e: IllegalArgumentException) {
                RealTimeLogger.w("ConfigInjection", "Rejected baseline_mode: ${e.message}")
            }
        }
        if (intent?.hasExtra("central_url") == true) {
            val u = intent.getStringExtra("central_url").orEmpty()
            com.example.hfl_experiment.AppConfig.setCentralUrl(context, u)
            RealTimeLogger.i("ConfigInjection", "Injected: central_url=${u.trim()}")
        }
        val hflMode = intent?.getStringExtra("hfl_mode")?.trim()
        if (!hflMode.isNullOrEmpty()) {
            try {
                com.example.hfl_experiment.AppConfig.setHflMode(context, hflMode)
                RealTimeLogger.i("ConfigInjection", "Injected: hfl_mode=$hflMode")
            } catch (e: IllegalArgumentException) {
                RealTimeLogger.w("ConfigInjection", "Rejected hfl_mode: ${e.message}")
            }
        }
        if (intent?.hasExtra("num_cycles") == true) {
            val n = intent.getIntExtra("num_cycles", -1)
            try {
                com.example.hfl_experiment.AppConfig.setNumCycles(context, n)
                RealTimeLogger.i("ConfigInjection", "Injected: num_cycles=$n")
            } catch (e: IllegalArgumentException) {
                RealTimeLogger.w("ConfigInjection", "Rejected num_cycles: ${e.message}")
            }
        }
        if (intent?.getBooleanExtra("clear_manual_config", false) == true) {
            com.example.hfl_experiment.AppConfig.clearManualConfig(context)
            com.example.hfl_experiment.AppConfig.autoConfigureFromIp(context)
            RealTimeLogger.i("ConfigInjection", "clear_manual_config: manual mark removed, IP auto config retried")
        }
        if (terminalId.isNullOrEmpty() && edgeUrl.isNullOrEmpty() && !hasDemoMode) return

        val prefs = context.getSharedPreferences("env_prefs", Context.MODE_PRIVATE)
        val editor = prefs.edit()

        if (!terminalId.isNullOrEmpty()) {
            editor.putString("terminal_id", terminalId)
            editor.putBoolean("terminal_identity_configured", true)
        }
        if (!edgeUrl.isNullOrEmpty()) {
            editor.putString("edge_host", edgeUrl)
            editor.putBoolean("edge_endpoint_configured", true)
            // ポートを明示指定したのでSSIDによる上書きを無効化
            editor.putBoolean("edge_ssid_port_rewrite", false)
        }
        var demoMode: Boolean? = null
        if (hasDemoMode) {
            demoMode = intent?.getBooleanExtra("demo_mode", false) ?: false
            editor.putBoolean("demo_mode", demoMode)
        }

        editor.apply()
        // SP3d 4.2: 端末 ID かエッジ URL を注入したら手動設定（demo_mode・モード・サイクル数だけなら付けない）
        if (!terminalId.isNullOrEmpty() || !edgeUrl.isNullOrEmpty()) {
            com.example.hfl_experiment.AppConfig.markManualConfig(context)
        }
        RealTimeLogger.i(
            "ConfigInjection",
            "Injected: terminal_id=$terminalId, edge_url=$edgeUrl, demo_mode=$demoMode"
        )
    }
}
