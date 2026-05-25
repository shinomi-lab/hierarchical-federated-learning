package com.example.hfl_experiment.ui

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiManager
import android.net.wifi.WifiNetworkSpecifier
import android.os.Build
import android.provider.Settings
import android.widget.Toast
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.example.hfl_experiment.network.requestNetworkAndRun
import com.example.hfl_experiment.network.NetworkClient
import com.example.hfl_experiment.network.ModelUpdateUtil
import com.example.hfl_experiment.AppConfig
import okhttp3.OkHttpClient
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.withContext
import org.json.JSONObject


/**
 * A lightweight composable button that triggers an immediate "forced" network switch trial.
 * - Does not remove or touch existing UI components; it runs its own trial and shows a simple progress dialog.
 * - Parameters:
 *   - networkClient: NetworkClient instance used to call server endpoints within the selected network.
 *   - wsClient: OkHttpClient used for downloads when needed.
 *   - downloadBaseUrl: base URL to resolve relative model links.
 */
@Composable
fun ForcedSwitchButton(
    modifier: Modifier = Modifier,
    networkClient: NetworkClient,
    wsClient: OkHttpClient,
    downloadBaseUrl: String,
    ssidA: String? = null,
    ssidB: String? = null,
    enableAutoTrial: Boolean = false
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var running by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf("処理中...") }
    var jobRef by remember { mutableStateOf<Job?>(null) }

    // --- new: load SSIDs from assets/ap_config.json for manual selection ---
    val availableSsids = remember {
        try {
            val isr = context.assets.open("ap_config.json")
            val txt = isr.bufferedReader().use { it.readText() }
            // ap_config.json in assets may contain comments; try to strip leading '//' lines
            val cleaned = txt.lines().filter { !it.trim().startsWith("//") }.joinToString("\n")
            val jo = JSONObject(cleaned)
            val list = mutableListOf<String>()
            jo.optJSONObject("apA")?.optString("ssid")?.takeIf { it.isNotBlank() }?.let { list.add(it) }
            jo.optJSONObject("apB")?.optString("ssid")?.takeIf { it.isNotBlank() }?.let { list.add(it) }
            list.toList()
        } catch (e: Exception) {
            emptyList<String>()
        }
    }
    var selectedSsid by remember { mutableStateOf<String?>(null) }

    fun getCurrentSsid(ctx: Context): String? {
        return try {
            val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
            val active = cm.activeNetwork
            if (active != null) {
                val caps = cm.getNetworkCapabilities(active)
                if (caps?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true) {
                    val wm = ctx.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
                    val ssid = wm.connectionInfo?.ssid
                    // normalize: strip quotes and lowercase
                    ssid?.trim('"')?.trim()?.lowercase()
                } else null
            } else null
        } catch (_: Exception) {
            null
        }
    }

    fun normalizeSsid(s: String?): String? = s?.trim('"')?.trim()?.lowercase()

    fun pickTargetSsid(ctx: Context, a: String?, b: String?): String? {
        // if user manually selected, prefer that
        selectedSsid?.let { return it }

        val cur = getCurrentSsid(ctx)
        val aa = a?.takeIf { it.isNotBlank() }
        val bb = b?.takeIf { it.isNotBlank() }
        val curN = normalizeSsid(cur)
        val aaN = normalizeSsid(aa)
        val bbN = normalizeSsid(bb)
        return when {
            aaN == null && bbN == null -> null
            curN == null -> aa ?: bb
            aaN != null && curN == aaN -> bb ?: aa
            bbN != null && curN == bbN -> aa ?: bb
            else -> aa ?: bb
        }
    }

    // Minimal blocking connect to a specific SSID (API 29+) with timeout
    fun connectToSsid(ctx: Context, targetSsid: String, timeoutMs: Long): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            // Older API: rely on existing connection; return false to indicate not switched
            return false
        }
        return try {
            val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
            val spec = WifiNetworkSpecifier.Builder()
                .setSsid(targetSsid)
                // .setWpa2Passphrase(AppConfig.ROUTER_PASS_A or B) // optional if needed
                .build()
            val request = NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .setNetworkSpecifier(spec)
                .build()
            val latch = CountDownLatch(1)
            val cb = object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) {
                    latch.countDown()
                }
                override fun onUnavailable() {
                    latch.countDown()
                }
            }
            cm.requestNetwork(request, cb)
            val ok = latch.await(timeoutMs, TimeUnit.MILLISECONDS)
            try { cm.unregisterNetworkCallback(cb) } catch (_: Exception) {}
            ok
        } catch (_: Exception) {
            false
        }
    }

    Column(modifier = modifier) {
        // show manual selection UI if available
        if (availableSsids.isNotEmpty()) {
            Text("手動SSID選択:")
            availableSsids.forEach { s ->
                Row(modifier = Modifier.padding(vertical = 4.dp)) {
                    RadioButton(selected = (selectedSsid == s), onClick = { selectedSsid = s })
                    Text(modifier = Modifier.padding(start = 8.dp), text = s)
                }
            }
            Text(modifier = Modifier.padding(bottom = 8.dp), text = "※手動選択がない場合は自動で判定します")
        }

        Button(
            onClick = {
                if (running) {
                    Toast.makeText(context, "切替は既に実行中です", Toast.LENGTH_SHORT).show()
                    return@Button
                }
                val targetSsid = pickTargetSsid(context, ssidA, ssidB)
                if (targetSsid.isNullOrBlank()) {
                    Toast.makeText(context, "SSIDが未設定です（apA/apBを設定してください）", Toast.LENGTH_LONG).show()
                    return@Button
                }

                // 一時モード: 接続パネルの表示のみに絞る（バックグラウンドの探索／試行は停止）
                if (!enableAutoTrial) {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                        try {
                            val intent = android.content.Intent(Settings.Panel.ACTION_INTERNET_CONNECTIVITY)
                            intent.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
                            context.startActivity(intent)
                            Toast.makeText(context, "接続パネルを開きました。'${targetSsid}' へ接続を承認してください。", Toast.LENGTH_LONG).show()
                        } catch (_: Exception) {
                            Toast.makeText(context, "接続パネルの起動に失敗しました。設定アプリからWi‑Fiを切り替えてください。", Toast.LENGTH_LONG).show()
                        }
                    } else {
                        Toast.makeText(context, "Android 9以下は設定からWi‑Fiを切り替えてください（${targetSsid}）", Toast.LENGTH_LONG).show()
                    }
                    return@Button
                }

                // 既存の自動試行フロー（必要時のみ有効）
                running = true
                message = "強制切替: $targetSsid に接続を試行しています..."
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    try {
                        val intent = android.content.Intent(Settings.Panel.ACTION_INTERNET_CONNECTIVITY)
                        intent.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
                        context.startActivity(intent)
                    } catch (_: Exception) { }
                }
                jobRef = scope.launch(kotlinx.coroutines.Dispatchers.IO) {
                    try {
                        // ensure we are connected (or at least attempted) to target SSID before network-bound operations
                        val switched = connectToSsid(context, targetSsid, 8_000)
                        // Verify current SSID after attempt
                        val nowSsid = getCurrentSsid(context)
                        val okTarget = normalizeSsid(nowSsid) == normalizeSsid(targetSsid)
                        if (!okTarget) {
                            // still on original or unknown: abort early to avoid same-SSID retry
                            withContext(kotlinx.coroutines.Dispatchers.Main) {
                                Toast.makeText(context, "切替できませんでした（${nowSsid ?: "不明"}）。別SSIDへ接続できません。", Toast.LENGTH_LONG).show()
                                // Bring up panel again to help user approve the connection
                                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                                    try {
                                        val intent = android.content.Intent(Settings.Panel.ACTION_INTERNET_CONNECTIVITY)
                                        intent.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
                                        context.startActivity(intent)
                                    } catch (_: Exception) { }
                                }
                            }
                            return@launch
                        }
                         val res = try {
                             requestNetworkAndRun(context, android.net.NetworkCapabilities.TRANSPORT_WIFI, 12_000L) { client ->
                                 try {
                                     val dto = networkClient.fetchSendToDevice()
                                     val modelLink = dto.links?.model ?: dto.model?.path
                                     if (modelLink.isNullOrBlank()) return@requestNetworkAndRun false
                                     val ok = if (modelLink.startsWith("http://") || modelLink.startsWith("https://")) {
                                         ModelUpdateUtil.downloadAndApplyModelFromUrlWithLock(context, modelLink, client, AppConfig.getServerAuthToken(context), null).first
                                     } else {
                                         ModelUpdateUtil.downloadAndApplyModelWithLock(context, downloadBaseUrl, client, AppConfig.getServerAuthToken(context), modelLink, null).first
                                     }
                                     ok
                                 } catch (_: Exception) { false }
                             }
                         } catch (_: Exception) { null }
                         withContext(kotlinx.coroutines.Dispatchers.Main) {
                            if (res == true && switched && okTarget) {
                                Toast.makeText(context, "強制切替成功: $targetSsid に接続して取得に成功", Toast.LENGTH_LONG).show()
                            } else if (res == true) {
                                Toast.makeText(context, "取得成功（切替は未確証）: $targetSsid", Toast.LENGTH_LONG).show()
                             } else {
                                 Toast.makeText(context, "強制切替失敗またはタイムアウト: $targetSsid", Toast.LENGTH_LONG).show()
                             }
                         }
                     } finally {
                         withContext(kotlinx.coroutines.Dispatchers.Main) {
                             running = false
                             jobRef = null
                         }
                     }
                 }
             },
             modifier = Modifier
                 .fillMaxWidth()
                 .padding(vertical = 8.dp)
         ) {
             Text("強制切替（Compose）")
         }

         if (running) {
             AlertDialog(
                 onDismissRequest = { /* block */ },
                 title = { Text("ネットワーク切替を試行中") },
                 text = {
                     Column {
                         Text(message)
                         CircularProgressIndicator(modifier = Modifier.padding(top = 8.dp))
                     }
                 },
                 confirmButton = {
                     Button(onClick = {
                         jobRef?.cancel()
                         jobRef = null
                         running = false
                         Toast.makeText(context, "切替をキャンセルしました", Toast.LENGTH_SHORT).show()
                     }) { Text("キャンセル") }
                 }
             )
         }
     }
 }
