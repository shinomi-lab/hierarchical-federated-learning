package com.example.hfl_experiment.ui

import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.example.hfl_experiment.AppConfig
import com.example.hfl_experiment.util.logging.RealTimeLogger
import java.net.URI

/**
 * 初回起動時など、端末が接続するエッジサーバのホスト／ポートをユーザーに確認する画面。
 * [AppConfig.isEdgeEndpointUserConfirmed] が false のとき [MainActivity] から遷移する。
 */
class EdgeEndpointSetupActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val (initHost, initPort) = initialHostPort(this)

        setContent {
            MaterialTheme {
                val ctx = LocalContext.current
                val host = remember { mutableStateOf(initHost) }
                val port = remember { mutableStateOf(initPort) }

                Column(
                    modifier = Modifier
                        .padding(20.dp)
                        .verticalScroll(rememberScrollState())
                ) {
                    Text(
                        "エッジサーバへの接続先",
                        style = MaterialTheme.typography.titleLarge
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        "この端末から学習データやモデルを送る先のエッジの IP（またはホスト名）とポートを入力してください。実験 LAN のエッジ PC に合わせてください。",
                        style = MaterialTheme.typography.bodyMedium
                    )
                    Spacer(modifier = Modifier.height(16.dp))
                    OutlinedTextField(
                        value = host.value,
                        onValueChange = { host.value = it },
                        label = { Text("エッジの IP またはホスト名") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = port.value,
                        onValueChange = { port.value = it },
                        label = { Text("ポート") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text("よく使うポート", style = MaterialTheme.typography.labelMedium)
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        listOf("8001", "8002", "8003").forEach { p ->
                            TextButton(onClick = { port.value = p }) {
                                Text(p)
                            }
                        }
                    }
                    Spacer(modifier = Modifier.height(20.dp))

                    // --- Memory optimization toggle ---
                    val memOpt = remember { mutableStateOf(AppConfig.isMemoryOptEnabled(ctx)) }
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text("メモリ最適化", style = MaterialTheme.typography.bodyMedium)
                        androidx.compose.material3.Switch(
                            checked = memOpt.value,
                            onCheckedChange = {
                                memOpt.value = it
                                AppConfig.setMemoryOptEnabled(ctx, it)
                            }
                        )
                    }
                    Text(
                        if (memOpt.value) "ON: バッファ再利用でメモリ抑制" else "OFF: 毎回新規確保（比較用）",
                        style = MaterialTheme.typography.labelSmall
                    )

                    Spacer(modifier = Modifier.height(20.dp))
                    Button(
                        onClick = {
                            val h = host.value.trim()
                            val p = port.value.trim()
                            if (h.isEmpty()) {
                                Toast.makeText(ctx, "IP またはホスト名を入力してください", Toast.LENGTH_SHORT).show()
                                return@Button
                            }
                            saveAndContinue(ctx, h, p)
                        },
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("保存してアプリを開く")
                    }
                    Spacer(modifier = Modifier.height(8.dp))
                    Button(
                        onClick = {
                            useCompiledDefaultsAndContinue(ctx)
                        },
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("アプリに組み込んだ既定の接続先のまま進む")
                    }
                }
            }
        }
    }

    private fun saveAndContinue(context: android.content.Context, host: String, port: String) {
        val prefs = context.getSharedPreferences("env_prefs", MODE_PRIVATE)
        prefs.edit()
            .putString("edge_host", host)
            .putString("edge_port", port)
            .apply()
        AppConfig.setEdgeSsidPortRewriteEnabled(context, false)
        AppConfig.markEdgeEndpointUserConfirmed(context)
        try {
            RealTimeLogger.i("EdgeEndpointSetup", "edge_host=$host edge_port=$port (saved, SSID port rewrite off)")
        } catch (_: Exception) { }
        goNext()
    }

    private fun useCompiledDefaultsAndContinue(context: android.content.Context) {
        val prefs = context.getSharedPreferences("env_prefs", MODE_PRIVATE)
        prefs.edit()
            .remove("edge_host")
            .remove("edge_port")
            .apply()
        AppConfig.setEdgeSsidPortRewriteEnabled(context, true)
        AppConfig.markEdgeEndpointUserConfirmed(context)
        try {
            RealTimeLogger.i("EdgeEndpointSetup", "using compiled default: ${AppConfig.getCompiledDefaultEdgeBaseUrl(context)}")
        } catch (_: Exception) { }
        goNext()
    }

    private fun goNext() {
        if (isTaskRoot) {
            val intent = android.content.Intent(this, MainActivity::class.java)
            intent.putExtra("from_setup", true)
            startActivity(intent)
        }
        finish()
    }

    companion object {
        fun initialHostPort(context: android.content.Context): Pair<String, String> {
            val prefs = context.getSharedPreferences("env_prefs", MODE_PRIVATE)
            val eh = prefs.getString("edge_host", null)?.trim()
            val ep = prefs.getString("edge_port", null)?.trim()
            if (!eh.isNullOrBlank()) {
                if (eh.startsWith("http://", ignoreCase = true)
                    || eh.startsWith("https://", ignoreCase = true)
                ) {
                    return try {
                        val u = URI(eh)
                        val portStr = if (u.port != -1) u.port.toString() else (if (!ep.isNullOrBlank()) ep else defaultPortFromUri(u))
                        Pair(u.host ?: "", portStr)
                    } catch (_: Exception) {
                        Pair(eh, if (!ep.isNullOrBlank()) ep else "8001")
                    }
                }
                return Pair(eh, if (!ep.isNullOrBlank()) ep else "8001")
            }
            val compiled = AppConfig.getCompiledDefaultEdgeBaseUrl(context)
            return try {
                val u = URI(compiled)
                val portStr = if (u.port != -1) u.port.toString() else "8001"
                Pair(u.host ?: "", portStr)
            } catch (_: Exception) {
                Pair("", "8001")
            }
        }

        private fun defaultPortFromUri(u: URI): String =
            if (u.port != -1) u.port.toString() else "8001"
    }
}
