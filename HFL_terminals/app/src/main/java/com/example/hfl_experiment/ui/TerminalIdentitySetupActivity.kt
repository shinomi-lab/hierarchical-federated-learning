package com.example.hfl_experiment.ui

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.example.hfl_experiment.AppConfig
import com.example.hfl_experiment.util.logging.RealTimeLogger
import org.json.JSONArray

/**
 * 同一 APK を複数端末に入れるための初回（または再設定）画面。
 * 連合学習でサーバが識別する **terminal_id** と任意の **experiment_group** を端末上で確定する。
 *
 * [AppConfig.isTerminalIdentityConfigured] が false のとき [MainActivity] から遷移する。
 */
class TerminalIdentitySetupActivity : ComponentActivity() {

    @OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        setContent {
            MaterialTheme {
                val ctx = LocalContext.current
                val prefs = remember {
                    ctx.getSharedPreferences("env_prefs", Context.MODE_PRIVATE)
                }
                val initTid = remember {
                    prefs.getString("terminal_id", null)?.trim().orEmpty()
                }
                val initGrp = remember {
                    ctx.getSharedPreferences("experiment_context", Context.MODE_PRIVATE)
                        .getString("experiment_group", null)?.trim().orEmpty()
                }
                val terminalId = remember { mutableStateOf(if (initTid.isNotEmpty()) initTid else "terminal-00") }
                val experimentGroup = remember { mutableStateOf(initGrp) }

                // App type list from assets/app.json
                val appTypes = remember {
                    try {
                        val json = ctx.assets.open("app.json").bufferedReader().use { it.readText() }
                        val arr = JSONArray(json)
                        (0 until arr.length()).map { i ->
                            arr.getJSONObject(i).optString("appType", "app_$i")
                        }
                    } catch (_: Exception) {
                        listOf("ブラウザ", "動画", "通話", "配信")
                    }
                }
                val hflPrefs = remember {
                    ctx.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
                }
                val savedAppIdx = remember {
                    val idx = hflPrefs.getInt("assigned_app_index", -1)
                    if (idx in appTypes.indices) idx else -1
                }
                val selectedAppIndex = remember { mutableStateOf(savedAppIdx) }
                val dropdownExpanded = remember { mutableStateOf(false) }

                Column(
                    modifier = Modifier
                        .padding(20.dp)
                        .verticalScroll(rememberScrollState())
                ) {
                    Text("端末 ID（連合クライアント名）", style = MaterialTheme.typography.titleLarge)
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        "サーバの /receive_terminal_weights/{id} 等で使われる ID です。端末ごとに一意にしてください（例: terminal-00）。",
                        style = MaterialTheme.typography.bodyMedium
                    )
                    Spacer(modifier = Modifier.height(16.dp))
                    OutlinedTextField(
                        value = terminalId.value,
                        onValueChange = { terminalId.value = it },
                        label = { Text("terminal_id") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text("プリセット選択:", style = MaterialTheme.typography.labelMedium)
                    Spacer(modifier = Modifier.height(4.dp))
                    FlowRow(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalArrangement = Arrangement.spacedBy(4.dp)
                    ) {
                        val presets = listOf(
                            "terminal-01 (.11)" to "terminal-01",
                            "terminal-02 (.12)" to "terminal-02",
                            "terminal-03 (.13)" to "terminal-03",
                            "terminal-04 (.14)" to "terminal-04",
                        )
                        presets.forEach { (label, value) ->
                            if (terminalId.value == value) {
                                FilledTonalButton(onClick = { }) {
                                    Text(label)
                                }
                            } else {
                                OutlinedButton(onClick = { terminalId.value = value }) {
                                    Text(label)
                                }
                            }
                        }
                    }
                    Spacer(modifier = Modifier.height(12.dp))
                    OutlinedTextField(
                        value = experimentGroup.value,
                        onValueChange = { experimentGroup.value = it },
                        label = { Text("experiment_group（任意）") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )

                    Spacer(modifier = Modifier.height(20.dp))
                    Text("使用アプリ種別", style = MaterialTheme.typography.titleMedium)
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        "この端末がシミュレートするアプリの種類を選択してください。「ランダム」を選ぶと学習開始時に自動割り当てされます。",
                        style = MaterialTheme.typography.bodyMedium
                    )
                    Spacer(modifier = Modifier.height(8.dp))

                    ExposedDropdownMenuBox(
                        expanded = dropdownExpanded.value,
                        onExpandedChange = { dropdownExpanded.value = !dropdownExpanded.value }
                    ) {
                        OutlinedTextField(
                            value = if (selectedAppIndex.value < 0) "ランダム（自動割り当て）"
                                    else appTypes.getOrElse(selectedAppIndex.value) { "ランダム" },
                            onValueChange = {},
                            readOnly = true,
                            label = { Text("アプリ種別") },
                            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = dropdownExpanded.value) },
                            modifier = Modifier.menuAnchor().fillMaxWidth()
                        )
                        ExposedDropdownMenu(
                            expanded = dropdownExpanded.value,
                            onDismissRequest = { dropdownExpanded.value = false }
                        ) {
                            DropdownMenuItem(
                                text = { Text("ランダム（自動割り当て）") },
                                onClick = {
                                    selectedAppIndex.value = -1
                                    dropdownExpanded.value = false
                                }
                            )
                            appTypes.forEachIndexed { idx, name ->
                                DropdownMenuItem(
                                    text = { Text(name) },
                                    onClick = {
                                        selectedAppIndex.value = idx
                                        dropdownExpanded.value = false
                                    }
                                )
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(20.dp))
                    Button(
                        onClick = {
                            val tid = terminalId.value.trim()
                            if (tid.isEmpty()) {
                                Toast.makeText(ctx, "terminal_id を入力してください", Toast.LENGTH_SHORT).show()
                                return@Button
                            }
                            AppConfig.applyTerminalIdentity(ctx, tid, experimentGroup.value.trim())
                            // Save app type selection
                            val appIdx = selectedAppIndex.value
                            if (appIdx >= 0) {
                                hflPrefs.edit().putInt("assigned_app_index", appIdx).apply()
                            } else {
                                // ランダム: remove so TrainingViewModel picks randomly
                                hflPrefs.edit().remove("assigned_app_index").apply()
                            }
                            try {
                                val appLabel = if (appIdx >= 0) appTypes.getOrElse(appIdx) { "?" } else "random"
                                RealTimeLogger.i("TerminalIdentitySetup", "saved terminal_id=$tid group=${experimentGroup.value.trim()} app_type=$appLabel (idx=$appIdx)")
                            } catch (_: Exception) { }
                            goNext()
                        },
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("保存してアプリを開く")
                    }
                }
            }
        }
    }

    private fun goNext() {
        startActivity(
            Intent(this, MainActivity::class.java).addFlags(
                Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP
            )
        )
        finish()
    }
}
