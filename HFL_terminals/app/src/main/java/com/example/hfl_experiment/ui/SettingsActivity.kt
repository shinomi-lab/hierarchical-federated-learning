package com.example.hfl_experiment.ui

import android.content.Context
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.ui.platform.LocalContext
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.selection.selectable
import androidx.compose.material3.RadioButton
import androidx.compose.ui.Alignment
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.OutlinedTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.text.input.KeyboardType
import android.widget.Toast
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

        setContent {
            val ctx = LocalContext.current
            val baselineMode = remember { mutableStateOf(AppConfig.appBaselineMode) }  // 端末設定（サーバ指定は別）
            val hflMode = remember { mutableStateOf(AppConfig.appHflMode) }  // 端末設定（/meta の hfl_mode が優先）
            val numCyclesText = remember { mutableStateOf(AppConfig.appNumCycles.toString()) }  // 端末設定（/meta の num_cycles が優先）
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


            Column(modifier = Modifier.verticalScroll(rememberScrollState()).padding(16.dp)) {
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

                Text("ベースライン実験モード", modifier = Modifier.padding(top = 16.dp))
                for ((mode, label) in BASELINE_MODE_LABELS) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.fillMaxWidth().selectable(selected = baselineMode.value == mode, onClick = { baselineMode.value = mode }),
                    ) {
                        RadioButton(selected = baselineMode.value == mode, onClick = { baselineMode.value = mode })
                        Text(label)
                    }
                }

                Text("HFL モード（エッジが hfl_mode を返すときはサーバ優先）", modifier = Modifier.padding(top = 16.dp))
                for ((mode, label) in HFL_MODE_LABELS) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.fillMaxWidth().selectable(selected = hflMode.value == mode, onClick = { hflMode.value = mode }),
                    ) {
                        RadioButton(selected = hflMode.value == mode, onClick = { hflMode.value = mode })
                        Text(label)
                    }
                }

                OutlinedTextField(
                    value = numCyclesText.value,
                    onValueChange = { numCyclesText.value = it },
                    label = { Text(NUM_CYCLES_LABEL) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth().padding(top = 16.dp),
                )

                Button(onClick = {
                    // SP3d: サイクル数は 1〜100 の整数のときだけ保存する（不正なら何も保存せず画面に留まる）
                    val numCycles = numCyclesText.value.trim().toIntOrNull()
                    if (!AppConfig.isValidNumCycles(numCycles)) {
                        Toast.makeText(ctx, "サイクル数は ${AppConfig.NUM_CYCLES_MIN}〜${AppConfig.NUM_CYCLES_MAX} の整数で入力してください", Toast.LENGTH_SHORT).show()
                        return@Button
                    }
                    // 保存
                    AppConfig.setBaselineMode(ctx, baselineMode.value)
                    AppConfig.setHflMode(ctx, hflMode.value)
                    AppConfig.setNumCycles(ctx, numCycles!!)
                    // SP3d 4.2: エッジの Host/Port を開いたときから変えたときだけ手動設定にする（モード・サイクル数だけでは付けない）
                    if (edgeHost.value != initEdgeHost || edgePort.value != initEdgePort) AppConfig.markManualConfig(ctx)
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


                    finish()
                }, modifier = Modifier.padding(top = 16.dp)) {
                    Text("保存して戻る")
                }
            }
        }
    }
}

private const val NUM_CYCLES_LABEL = "1 セッションのサイクル数（1〜100、サーバ指定が優先）"

private val HFL_MODE_LABELS = listOf(
    "sync" to "sync（毎サイクルラウンド進行を待つ）",
    "async" to "async（閾値ベース非同期）",
)

private val BASELINE_MODE_LABELS = listOf(
    "none" to "none（通常のHFL学習）",
    "fixed" to "fixed（常にAP0・学習しない）",
    "fixed_ap1" to "fixed_ap1（常にAP1・学習しない）",
    "random" to "random（セッションごとにランダムなAP・学習しない）",
    "greedy" to "greedy（満足度最大AP・学習しない）",
    "central" to "central（中央へ問い合わせ・学習しない・推奨APは記録のみ）",
    "hfl_rec" to "hfl_rec（HFLの最新モデルの推奨APを問い合わせ・学習しない・切替を提案）",
)
