package com.example.hfl_experiment.ui

import android.content.Context
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.ui.platform.LocalContext
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.example.hfl_experiment.AppConfig
import android.content.Intent
import android.provider.Settings

class SettingsActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefsNet = getSharedPreferences("network_prefs", Context.MODE_PRIVATE)
        val prefsEnv = getSharedPreferences("env_prefs", Context.MODE_PRIVATE)
        val uiPrefs = getSharedPreferences("ui_prefs", Context.MODE_PRIVATE)

        val initSsidA = prefsNet.getString("ssidA", "") ?: ""
        val initPassA = prefsNet.getString("passA", "") ?: ""
        val initSsidB = prefsNet.getString("ssidB", "") ?: ""
        val initPassB = prefsNet.getString("passB", "") ?: ""
        val initBssidA = prefsNet.getString("bssidA", "") ?: ""
        val initRouterIpA = prefsNet.getString("routerIpA", "") ?: ""
        val initBssidB = prefsNet.getString("bssidB", "") ?: ""
        val initRouterIpB = prefsNet.getString("routerIpB", "") ?: ""

        // derive default host/port from AppConfig (which falls back to BuildConfig or DEFAULT in AppConfig)
        val defaultBase = AppConfig.getEdgeBaseUrl(this).removePrefix("http://").removePrefix("https://").removePrefix("ws://").removePrefix("wss://").trimEnd('/')
        val defaultHost = if (defaultBase.contains(":")) defaultBase.substringBefore(":") else defaultBase
        val defaultPort = if (defaultBase.contains(":")) defaultBase.substringAfter(":") else "8001"

        val initEdgeHost = prefsEnv.getString("edge_host", defaultHost) ?: defaultHost
        val initEdgePort = prefsEnv.getString("edge_port", defaultPort) ?: defaultPort
        val initAutoNotify = prefsEnv.getBoolean("auto_notification", false)

        // UI prefs for tool pocket
        val initPocketValidate = uiPrefs.getBoolean("pocket_validate_dto", true)
        val initPocketUpload = uiPrefs.getBoolean("pocket_upload_metrics", true)
        val initPocketReset = uiPrefs.getBoolean("pocket_reset_round", true)
        val initPocketFetch = uiPrefs.getBoolean("pocket_fetch_model", true)
        val initPocketDebugUpload = uiPrefs.getBoolean("pocket_debug_upload", false)
        val initPocketDebugDiagnose = uiPrefs.getBoolean("pocket_debug_diagnose", false)
        val initPocketSettingsButton = uiPrefs.getBoolean("pocket_settings_button", true)
        val initToolsExpanded = uiPrefs.getBoolean("tools_expanded", false)

        setContent {
            val ctx = LocalContext.current
            val ssidA = remember { mutableStateOf(initSsidA) }
            val passA = remember { mutableStateOf(initPassA) }
            val bssidA = remember { mutableStateOf(initBssidA) }
            val routerIpA = remember { mutableStateOf(initRouterIpA) }

            val ssidB = remember { mutableStateOf(initSsidB) }
            val passB = remember { mutableStateOf(initPassB) }
            val bssidB = remember { mutableStateOf(initBssidB) }
            val routerIpB = remember { mutableStateOf(initRouterIpB) }

            val edgeHost = remember { mutableStateOf(initEdgeHost) }
            val edgePort = remember { mutableStateOf(initEdgePort) }
            val autoNotify = remember { mutableStateOf(initAutoNotify) }

            // UI pocket toggles
            val pocketValidate = remember { mutableStateOf(initPocketValidate) }
            val pocketUploadMetrics = remember { mutableStateOf(initPocketUpload) }
            val pocketResetRound = remember { mutableStateOf(initPocketReset) }
            val pocketFetchModel = remember { mutableStateOf(initPocketFetch) }
            val pocketDebugUpload = remember { mutableStateOf(initPocketDebugUpload) }
            val pocketDebugDiagnose = remember { mutableStateOf(initPocketDebugDiagnose) }
            val pocketSettingsButton = remember { mutableStateOf(initPocketSettingsButton) }
            val toolsExpandedPref = remember { mutableStateOf(initToolsExpanded) }

            Column(modifier = Modifier.padding(16.dp)) {
                Text("環境設定")
                Button(
                    onClick = {
                        ctx.startActivity(Intent(ctx, EdgeEndpointSetupActivity::class.java))
                    },
                    modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp)
                ) {
                    Text("エッジ接続先のみを確認・変更")
                }
                Button(
                    onClick = {
                        ctx.startActivity(Intent(ctx, TerminalIdentitySetupActivity::class.java))
                    },
                    modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp)
                ) {
                    Text("端末 ID / 実験群を変更")
                }
                Text("エッジサーバ (Host/Port)")
                OutlinedTextField(value = edgeHost.value, onValueChange = { edgeHost.value = it }, label = { Text("Edge Host") }, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(value = edgePort.value, onValueChange = { edgePort.value = it }, label = { Text("Edge Port") }, modifier = Modifier.fillMaxWidth())
                Row(modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp)) {
                    Checkbox(checked = autoNotify.value, onCheckedChange = { autoNotify.value = it })
                    Button(onClick = { ctx.startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }, modifier = Modifier.padding(start = 8.dp)) {
                        Text("通知アクセス設定を開く")
                    }
                }

                // UI / Tools pocket settings
                Text("\nUI 設定（ツールポケット）")
                RowLabeledCheckbox("展開状態を初期表示で開く", toolsExpandedPref.value) { toolsExpandedPref.value = it }
                RowLabeledCheckbox("Validate DTO をポケットに表示", pocketValidate.value) { pocketValidate.value = it }
                RowLabeledCheckbox("Upload Metrics を表示", pocketUploadMetrics.value) { pocketUploadMetrics.value = it }
                RowLabeledCheckbox("Reset Round を表示", pocketResetRound.value) { pocketResetRound.value = it }
                RowLabeledCheckbox("Fetch New Model を表示", pocketFetchModel.value) { pocketFetchModel.value = it }
                RowLabeledCheckbox("DEBUG: Test Upload を表示", pocketDebugUpload.value) { pocketDebugUpload.value = it }
                RowLabeledCheckbox("DEBUG: Network Diagnose を表示", pocketDebugDiagnose.value) { pocketDebugDiagnose.value = it }
                RowLabeledCheckbox("設定ボタンをポケット内に表示", pocketSettingsButton.value) { pocketSettingsButton.value = it }

                Text("ルータA (SSID/Pass/BSSID/IP)")
                OutlinedTextField(value = ssidA.value, onValueChange = { ssidA.value = it }, label = { Text("SSID A") }, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(value = passA.value, onValueChange = { passA.value = it }, label = { Text("Pass A") }, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(value = bssidA.value, onValueChange = { bssidA.value = it }, label = { Text("BSSID A (任意)") }, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(value = routerIpA.value, onValueChange = { routerIpA.value = it }, label = { Text("Router IP A (任意)") }, modifier = Modifier.fillMaxWidth())

                Text("ルータB (SSID/Pass/BSSID/IP)")
                OutlinedTextField(value = ssidB.value, onValueChange = { ssidB.value = it }, label = { Text("SSID B") }, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(value = passB.value, onValueChange = { passB.value = it }, label = { Text("Pass B") }, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(value = bssidB.value, onValueChange = { bssidB.value = it }, label = { Text("BSSID B (任意)") }, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(value = routerIpB.value, onValueChange = { routerIpB.value = it }, label = { Text("Router IP B (任意)") }, modifier = Modifier.fillMaxWidth())

                Button(onClick = {
                    // 保存
                    prefsEnv.edit()
                        .putString("edge_host", edgeHost.value)
                        .putString("edge_port", edgePort.value)
                        .putBoolean("auto_notification", autoNotify.value)
                        .apply()
                    AppConfig.setEdgeSsidPortRewriteEnabled(ctx, false)
                    AppConfig.markEdgeEndpointUserConfirmed(ctx)

                    prefsNet.edit()
                        .putString("ssidA", ssidA.value)
                        .putString("passA", passA.value)
                        .putString("bssidA", bssidA.value)
                        .putString("routerIpA", routerIpA.value)
                        .putString("ssidB", ssidB.value)
                        .putString("passB", passB.value)
                        .putString("bssidB", bssidB.value)
                        .putString("routerIpB", routerIpB.value)
                        .apply()

                    // save UI prefs
                    uiPrefs.edit()
                        .putBoolean("pocket_validate_dto", pocketValidate.value)
                        .putBoolean("pocket_upload_metrics", pocketUploadMetrics.value)
                        .putBoolean("pocket_reset_round", pocketResetRound.value)
                        .putBoolean("pocket_fetch_model", pocketFetchModel.value)
                        .putBoolean("pocket_debug_upload", pocketDebugUpload.value)
                        .putBoolean("pocket_debug_diagnose", pocketDebugDiagnose.value)
                        .putBoolean("pocket_settings_button", pocketSettingsButton.value)
                        .putBoolean("tools_expanded", toolsExpandedPref.value)
                        .apply()

                    finish()
                }, modifier = Modifier.padding(top = 16.dp)) {
                    Text("保存して戻る")
                }
            }
        }
    }
}

@Composable
private fun RowLabeledCheckbox(label: String, checked: Boolean, onCheckedChange: (Boolean) -> Unit) {
    Row(modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp)) {
        Checkbox(checked = checked, onCheckedChange = onCheckedChange)
        Text(label, modifier = Modifier.padding(start = 8.dp))
    }
}
