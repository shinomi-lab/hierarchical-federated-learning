package com.example.hfl_experiment.ui

import android.app.Activity
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.example.hfl_experiment.AppConfig
import com.example.hfl_experiment.experiment.ExperimentContext
import com.example.hfl_experiment.network.NetworkClient
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.TextButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem

/**
 * Simple activity: list files under filesDir/log, allow multi-select and upload to server via NetworkClient.uploadClientLogs
 */
class LogSenderActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                LogSenderScreen(activity = this)
            }
        }
    }
}

@Composable
fun LogSenderScreen(activity: Activity) {
    val ctx = activity.applicationContext
    val filesDir = ctx.filesDir
    val logDir = File(filesDir, "log")
    var files by remember { mutableStateOf(listOf<File>()) }
    var selected by remember { mutableStateOf(setOf<String>()) }
    var uploading by remember { mutableStateOf(false) }
    var uploadResult by remember { mutableStateOf<String?>(null) }
    var sortMode by remember { mutableStateOf("date_desc") } // options: date_desc, date_asc, name_asc, name_desc, size_desc, size_asc
    var daysFilterText by remember { mutableStateOf("") } // empty = no filter; numeric value = last N days
    val scope = rememberCoroutineScope()

    LaunchedEffect(Unit) {
        val list = withContext(Dispatchers.IO) {
            if (logDir.exists() && logDir.isDirectory) logDir.listFiles()?.filter { it.isFile }?.toList() ?: emptyList() else emptyList()
        }
        files = list.sortedByDescending { it.lastModified() }
    }

    // Compute visible files applying days filter and sort
    val visibleFiles by remember(files, sortMode, daysFilterText) {
        derivedStateOf {
            var list = files
            // days filter
            val days = daysFilterText.toIntOrNull()
            if (days != null && days > 0) {
                val cutoff = System.currentTimeMillis() - days * 24L * 3600L * 1000L
                list = list.filter { it.lastModified() >= cutoff }
            }
            // sort
            list = when (sortMode) {
                "date_asc" -> list.sortedBy { it.lastModified() }
                "date_desc" -> list.sortedByDescending { it.lastModified() }
                "name_asc" -> list.sortedBy { it.name.lowercase() }
                "name_desc" -> list.sortedByDescending { it.name.lowercase() }
                "size_asc" -> list.sortedBy { it.length() }
                "size_desc" -> list.sortedByDescending { it.length() }
                else -> list
            }
            list
        }
    }

    Column(modifier = Modifier.fillMaxSize().padding(8.dp)) {
        Text("Log files (${files.size})", modifier = Modifier.padding(4.dp))
        // Controls: select all / deselect all, sort, days filter
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { selected = visibleFiles.map { it.absolutePath }.toSet() }) { Text("Select Visible") }
            Button(onClick = { selected = emptySet() }) { Text("Deselect All") }
            Spacer(modifier = Modifier.weight(1f))
            OutlinedTextField(value = daysFilterText, onValueChange = { daysFilterText = it }, label = { Text("Last N days") }, modifier = Modifier.width(140.dp))
        }
        Row(modifier = Modifier.fillMaxWidth().padding(top = 4.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Sort:")
            DropdownSortMenu(current = sortMode, onSelect = { sortMode = it })
        }

        LazyColumn(modifier = Modifier.weight(1f)) {
            items(visibleFiles) { f ->
                Row(modifier = Modifier.fillMaxWidth().padding(6.dp)) {
                    val checked = selected.contains(f.absolutePath)
                    Checkbox(checked = checked, onCheckedChange = { ch ->
                        selected = if (ch) selected + f.absolutePath else selected - f.absolutePath
                    })
                    Spacer(modifier = Modifier.width(8.dp))
                    Column(modifier = Modifier.clickable {
                        selected = if (selected.contains(f.absolutePath)) selected - f.absolutePath else selected + f.absolutePath
                    }) {
                        Text(f.name)
                        Text("${f.length()} bytes • ${java.text.SimpleDateFormat("yyyy-MM-dd HH:mm", java.util.Locale.US).format(java.util.Date(f.lastModified()))}", style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        }

        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Button(onClick = {
                // refresh
                scope.launch {
                    val list = withContext(Dispatchers.IO) { if (logDir.exists()) logDir.listFiles()?.filter { it.isFile }?.toList() ?: emptyList() else emptyList() }
                    files = list
                }
            }) { Text("Refresh") }

            Button(onClick = {
                if (selected.isEmpty()) { Toast.makeText(activity, "Select at least one file", Toast.LENGTH_SHORT).show(); return@Button }
                uploading = true
                scope.launch {
                    try {
                        val baseUrl = com.example.hfl_experiment.AppConfig.getEdgeBaseUrl(ctx)
                        val token = com.example.hfl_experiment.AppConfig.getServerAuthToken(ctx)
                        val client = NetworkClient(ctx, baseUrl, authToken = token)
                         val map = mutableMapOf<String, File>()
                         var i = 0
                         // preserve order: send visible files first if selected
                         val toSend = visibleFiles.map { it.absolutePath }.filter { selected.contains(it) } + selected.filter { sp -> !visibleFiles.any { it.absolutePath == sp } }
                         for (p in toSend) {
                             val f = File(p)
                             if (f.exists() && f.isFile && f.length() > 0L) {
                                 map["file_${i}"] = f
                                 i++
                             }
                         }
                        if (map.isEmpty()) {
                            uploading = false
                            uploadResult = "Upload skipped: selected files are empty or missing."
                            return@launch
                        }
                        // Attempt to include authoritative server round to avoid misc classification on server
                        val metaJson = try {
                            val meta = client.fetchMeta(forceRefresh = true)
                            val jo = JSONObject()
                            jo.put("terminal_id", AppConfig.getTerminalId(ctx))
                            jo.put("round", meta.round)
                            jo.put("timestamp_ms", System.currentTimeMillis())
                            val rid = ExperimentContext.runId
                            if (rid.isNotBlank()) jo.put("run_id", rid)
                            jo.toString()
                        } catch (e: Exception) {
                            val jo = JSONObject()
                            jo.put("terminal_id", AppConfig.getTerminalId(ctx))
                            jo.put("timestamp_ms", System.currentTimeMillis())
                            val rid = ExperimentContext.runId
                            if (rid.isNotBlank()) jo.put("run_id", rid)
                            jo.toString()
                        }
                        val resp = client.uploadClientLogs(AppConfig.getTerminalId(ctx), map, metaJson)
                        // Try to parse server JSON and build a friendly summary
                        val summary = try {
                            val jo = JSONObject(resp)
                            val ack = jo.optBoolean("ack", false)
                            val status = jo.optString("status", "")
                            val received = jo.optJSONArray("received")
                            val sb = StringBuilder()
                            sb.appendLine("ack: $ack, status: $status")
                            if (received != null) {
                                for (i in 0 until received.length()) {
                                    val item = received.optJSONObject(i) ?: continue
                                    val name = item.optString("name")
                                    val size = item.optLong("size")
                                    val sha = item.optString("sha256")
                                    sb.appendLine("- $name (${size} bytes) ${if (sha.isNotBlank()) "sha256=${sha}" else ""}")
                                }
                            } else {
                                sb.appendLine(resp.take(800))
                            }
                            sb.toString()
                        } catch (_: Exception) {
                            // Not JSON or parse error — show raw response trimmed
                            resp.take(2000)
                        }
                        uploadResult = "Upload succeeded:\n" + summary
                     } catch (e: Exception) {
                        uploadResult = "Upload failed: ${e.message}"
                     } finally {
                         uploading = false
                     }
                 }
             }) { Text(if (uploading) "Uploading..." else "Upload Selected") }
         }
     }

    // Show result dialog when uploadResult is set
    if (uploadResult != null) {
        AlertDialog(onDismissRequest = { uploadResult = null }, title = { Text("Upload Result") }, text = { Text(uploadResult ?: "") }, confirmButton = {
            TextButton(onClick = { uploadResult = null }) { Text("OK") }
        })
    }
}

@Composable
private fun DropdownSortMenu(current: String, onSelect: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    Box {
        Button(onClick = { expanded = true }) { Text("${when(current){"date_desc"->"Date ▼";"date_asc"->"Date ▲";"name_asc"->"Name ▲";"name_desc"->"Name ▼";"size_desc"->"Size ▼";"size_asc"->"Size ▲"; else->"Sort"}}") }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            DropdownMenuItem(text = { Text("Date ▼") }, onClick = { onSelect("date_desc"); expanded = false })
            DropdownMenuItem(text = { Text("Date ▲") }, onClick = { onSelect("date_asc"); expanded = false })
            DropdownMenuItem(text = { Text("Name ▲") }, onClick = { onSelect("name_asc"); expanded = false })
            DropdownMenuItem(text = { Text("Name ▼") }, onClick = { onSelect("name_desc"); expanded = false })
            DropdownMenuItem(text = { Text("Size ▲") }, onClick = { onSelect("size_asc"); expanded = false })
            DropdownMenuItem(text = { Text("Size ▼") }, onClick = { onSelect("size_desc"); expanded = false })
        }
    }
}
