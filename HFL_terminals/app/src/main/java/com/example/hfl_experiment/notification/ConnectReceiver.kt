package com.example.hfl_experiment.notification

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.example.hfl_experiment.experiment.NetworkManager
import com.example.hfl_experiment.util.logging.RealTimeLogger
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

class ConnectReceiver : BroadcastReceiver() {
    private val TAG = "ConnectReceiver"

    override fun onReceive(context: Context, intent: Intent?) {
        if (intent == null) return
        val apId = intent.getStringExtra("ap_id") ?: return
        val ssid = intent.getStringExtra("ap_ssid") ?: apId
        RealTimeLogger.i(TAG, "ConnectReceiver received request apId=$apId ssid=$ssid")

        // perform connection asynchronously using NetworkManager
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val nm = NetworkManager(context)
                // Load configured APs from assets and apply any overrides from SharedPreferences
                val cfg = try { com.example.hfl_experiment.experiment.Config.loadConfigFromAssets(context) } catch (_: Exception) { null }
                var pskFromConfig: String? = null
                cfg?.let {
                    val apCfg = if (apId == it.apA.id) it.apA else if (apId == it.apB.id) it.apB else null
                    pskFromConfig = apCfg?.psk
                }
                // allow user overrides in SharedPreferences (network_prefs)
                try {
                    val prefs = context.getSharedPreferences("network_prefs", Context.MODE_PRIVATE)
                    val key = if (apId == cfg?.apA?.id) "passA" else if (apId == cfg?.apB?.id) "passB" else null
                    if (key != null) {
                        val override = prefs.getString(key, null)
                        if (!override.isNullOrBlank()) pskFromConfig = override
                    }
                } catch (_: Exception) { }

                val ap = com.example.hfl_experiment.experiment.ApConfig(id = apId, ssid = ssid, psk = pskFromConfig ?: "")
                val ok = nm.connectTo(ap)
                RealTimeLogger.i(TAG, "connect attempt returned: $ok for $ssid")
                if (ok) {
                    // persist last selected index if possible
                    try {
                        val cfg2 = cfg
                        if (cfg2 != null) {
                            val idx = if (apId == cfg2.apA.id) 0 else if (apId == cfg2.apB.id) 1 else -1
                            if (idx >= 0) {
                                val prefs = context.getSharedPreferences("network_prefs", Context.MODE_PRIVATE)
                                prefs.edit().putInt("last_selected_index", idx).apply()
                            }
                        }
                    } catch (_: Exception) { }
                }
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "connect failed: ${e.message}")
            }
        }
    }
}
