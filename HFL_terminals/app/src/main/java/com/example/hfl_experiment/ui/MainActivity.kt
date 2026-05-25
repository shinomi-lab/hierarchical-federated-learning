@file:SuppressLint("UnprotectedBroadcastReceiver")

package com.example.hfl_experiment.ui

import android.content.Intent
import android.content.BroadcastReceiver
import android.content.IntentFilter
import com.example.hfl_experiment.util.registerReceiverNotExported
import androidx.lifecycle.ViewModelProvider
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Scaffold
import androidx.compose.ui.Alignment
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Text
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.lifecycleScope
import com.example.hfl_experiment.AppConfig
import com.example.hfl_experiment.network.NetworkClient
import com.example.hfl_experiment.BuildConfig
import com.example.hfl_experiment.network.util.PingUtil
import com.example.hfl_experiment.training.TrainingViewModel
import com.example.hfl_experiment.ui.theme.HFL_experimentTheme
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkInfo
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import androidx.compose.runtime.collectAsState
import android.provider.Settings
import android.net.Uri
import kotlinx.coroutines.*
import kotlin.random.Random
import java.io.File
import android.annotation.SuppressLint
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.jsonArray
import android.content.Context
import com.example.hfl_experiment.worker.UploadMetricsWorker
import okhttp3.Response
import okio.ByteString
import com.example.hfl_experiment.model.AppConfig as AppConfigModel
import com.example.hfl_experiment.network.ModelUpdateUtil
import java.util.concurrent.TimeUnit
import com.example.hfl_experiment.util.networking.SatisfactionUtil
import com.example.hfl_experiment.util.networking.NetworkBandwidthProbe
import com.example.hfl_experiment.util.networking.NetworkSwitchHelper
import com.example.hfl_experiment.util.logging.RealTimeLogger
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.RequestBody.Companion.toRequestBody
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.os.Build
import android.Manifest
import android.content.pm.PackageManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import java.util.Locale


class WebSocketListenerImpl(
    private val wsUrl: String,
    private val downloadBaseUrl: String,
    private val context: Context,
    private val client: OkHttpClient,
    private val authToken: String?,
    private val viewModel: TrainingViewModel? = null
) : WebSocketListener() {
    // reconnect state
    private var reconnectAttempts = 0
    private val maxReconnectAttempts = 6
    private val baseBackoffMs = 1000L

    // Stop flag: once set, this listener will NOT reconnect or process messages.
    // Used to cleanly retire an old listener when creating a new WebSocket connection.
    @Volatile
    private var stopped = false

    /**
     * Permanently stop this listener. After calling this:
     * - onFailure/onClosed will NOT trigger reconnection
     * - The underlying WebSocket is closed gracefully
     * - The keepalive ping loop will exit
     */
    fun stop() {
        stopped = true
        try {
            webSocketRef?.close(1000, "listener stopped")
        } catch (_: Exception) {}
        webSocketRef = null
        RealTimeLogger.i("WebSocket", "Listener stopped (zombie prevention)")
    }

    // hold a reference to the active WebSocket so we can send ACKs and pings
    @Volatile
    private var webSocketRef: WebSocket? = null

    // Listener-local helper: compute this terminal's index from AppConfig terminal id
    private fun myTerminalIndex(): Int? {
        return try {
            val tid = AppConfig.getTerminalId(context)
            val m = Regex("(\\d+)").find(tid ?: "")
            val num = m?.value?.toIntOrNull()
            num?.let { it - 1 }
        } catch (e: Exception) {
            RealTimeLogger.w("WebSocket", "myTerminalIndex failed: ${e.message}")
            null
        }
    }

    // Listener-local helper: map current connected SSID to AP index (0=primary,1=secondary, -1 unknown)
    private fun currentApIndex(): Int {
        return try {
            val currentSsid = NetworkSwitchHelper.getConnectedSsid(context)
            val a = AppConfig.getPrimarySsid(context)
            val b = AppConfig.getSecondarySsid(context)
            when {
                currentSsid == null -> -1
                currentSsid == a -> 0
                currentSsid == b -> 1
                else -> -1
            }
        } catch (e: Exception) {
            RealTimeLogger.w("WebSocket", "currentApIndex failed: ${e.message}")
            -1
        }
    }

    // Listener-local attempt switch using NetworkSwitchHelper; returns true if connected to desired SSID
    private suspend fun attemptSwitchLocal(targetIndex: Int, maxAttempts: Int = 3, initialBackoffMs: Long = 1000L): Boolean {
        return withContext(Dispatchers.IO) {
            try {
                val ssid = when (targetIndex) {
                    0 -> AppConfig.getPrimarySsid(context)
                    1 -> AppConfig.getSecondarySsid(context)
                    else -> null
                }
                val pass = when (targetIndex) {
                    0 -> AppConfig.ROUTER_PASS_A
                    1 -> AppConfig.ROUTER_PASS_B
                    else -> null
                }
                if (ssid.isNullOrBlank()) return@withContext false

                var attempt = 0
                var backoff = initialBackoffMs
                while (attempt < maxAttempts) {
                    attempt++
                    RealTimeLogger.i("WebSocket", "attemptSwitchLocal: attempt=${attempt} ssid=${ssid}")
                    val status = try { NetworkSwitchHelper.suggestWifi(context, ssid, pass) } catch (e: Exception) { -99 }
                    RealTimeLogger.i("WebSocket", "suggestWifi returned status=${status} for ssid=${ssid}")

                    val connected = try {
                        NetworkSwitchHelper.awaitWifiConnected(context, timeoutMs = 20_000L) { newSsid -> RealTimeLogger.i("WebSocket", "awaitWifiConnected update: ssid=${newSsid}") }
                    } catch (e: Exception) { false }

                    if (connected) {
                        val nowSsid = NetworkSwitchHelper.getConnectedSsid(context)
                        if (!nowSsid.isNullOrBlank() && nowSsid == ssid) {
                            RealTimeLogger.i("WebSocket", "Connected to target ssid=${ssid}")
                            // persist current ap index
                            try { context.applicationContext.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE).edit().putInt("current_ap_index", targetIndex).apply() } catch (_: Exception) {}
                            return@withContext true
                        }
                    }

                    try { Thread.sleep(backoff) } catch (_: InterruptedException) {}
                    backoff = (backoff * 2).coerceAtMost(30_000L)
                }

                RealTimeLogger.w("WebSocket", "attemptSwitchLocal: all attempts failed for ssid=${ssid}")
                return@withContext false
            } catch (e: Exception) {
                RealTimeLogger.w("WebSocket", "attemptSwitchLocal exception: ${e.message}")
                return@withContext false
            }
        }
    }

    override fun onOpen(webSocket: WebSocket, response: Response) {
        if (stopped) { webSocket.close(1000, "listener stopped"); return }
        RealTimeLogger.d("WebSocket", "Connected to server")
        // reset attempts on successful open
        reconnectAttempts = 0
        webSocketRef = webSocket

        // start a lightweight keepalive ping loop to help detect dead sockets
        CoroutineScope(Dispatchers.IO).launch {
            try {
                while (webSocketRef != null && !stopped) {
                    delay(30_000) // 30s
                    if (stopped) break
                    try {
                        // send a minimal heartbeat; server should ignore unknown types
                        val pingJson = "{\"type\":\"ping\"}"
                        webSocketRef?.send(pingJson)
                        appendLog("PING_SENT\t${System.currentTimeMillis()}")
                    } catch (_: Exception) {
                        RealTimeLogger.w("WebSocket", "ping failed")
                    }
                }
            } catch (_: CancellationException) {
                // coroutine was cancelled, ignore
            } catch (_: Exception) {
                RealTimeLogger.w("WebSocket", "keepalive loop failed")
            }
        }
    }

    override fun onMessage(webSocket: WebSocket, text: String) {
        if (stopped) return
        val ts = System.currentTimeMillis()
        RealTimeLogger.d("WebSocket", "Message received at $ts: $text")
        // also persist a small debug trace to file for postmortem
        try {
            val trace = "RECV\t$ts\t$text\n"
            val logF = File(context.filesDir, "model_update_log.txt")
            logF.appendText(trace)
        } catch (e: Exception) {
            RealTimeLogger.w("WebSocket", "Failed to append recv trace: ${e.message}")
        }

        // サーバリセット通知を受信したら再接続を停止
        try {
            val json = org.json.JSONObject(text)
            if (json.optString("type") == "server_reset") {
                RealTimeLogger.i("WebSocket", "Received server_reset notification. Stopping reconnect.")
                stopped = true
                webSocketRef = null
                return
            }
        } catch (_: Exception) { /* not JSON or missing type — continue */ }

        CoroutineScope(Dispatchers.IO).launch {
            handleModelUpdate(text)
        }
    }

    // log binary frames too (some servers send binary payloads)
    override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
        val ts = System.currentTimeMillis()
        try {
            val byteArr = bytes.toByteArray()
            val hexPreview = byteArr.joinToString("") { "%02x".format(it) }.take(200)
            RealTimeLogger.d("WebSocket", "Binary message received at $ts: hex(preview)=$hexPreview")
            val trace = "RECV_BIN\t$ts\t${hexPreview}\n"
            val logF = File(context.filesDir, "model_update_log.txt")
            logF.appendText(trace)
        } catch (e: Exception) {
            RealTimeLogger.w("WebSocket", "Failed to append recv binary trace: ${e.message}")
        }
        // nothing more to do for binary frames unless server uses a binary-encoded JSON (protobuf)
    }

    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
        RealTimeLogger.w("WebSocket", "Socket closed: code=$code reason=$reason")
        webSocketRef = null
        appendLog("CLOSED\t${System.currentTimeMillis()}\tcode=$code reason=$reason")
        // サーバリセット(1012)の場合は再接続しない
        if (code == 1012 || reason.contains("reset", ignoreCase = true)) {
            RealTimeLogger.i("WebSocket", "Server reset detected (code=$code). Not reconnecting.")
            stopped = true
            return
        }
        if (stopped) return
        scheduleReconnect()
    }

    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
        val hint = if (t is java.net.ConnectException ||
            t.message?.contains("ECONNREFUSED", ignoreCase = true) == true
        ) {
            " [ヒント: エッジ起動・URL(ポート)/同一LAN・FWを確認]"
        } else ""
        RealTimeLogger.e("WebSocket", "Socket failure: ${t.message}$hint", t)
        webSocketRef = null
        appendLog("FAIL\t${System.currentTimeMillis()}\t${t.message}$hint")
        if (stopped) return
        scheduleReconnect()
    }

    private fun scheduleReconnect() {
        if (stopped) return
        try {
            if (reconnectAttempts >= maxReconnectAttempts) {
                RealTimeLogger.e("WebSocket", "Max reconnect attempts reached; not reconnecting further")
                appendLog("RECONNECT_GIVEUP\t${System.currentTimeMillis()}\tattempts=$reconnectAttempts")
                return
            }
            reconnectAttempts++
            val jitter = Random.nextLong(0, 500)
            val backoff = baseBackoffMs * (1L shl (reconnectAttempts - 1)) + jitter
            RealTimeLogger.i("WebSocket", "Scheduling reconnect attempt $reconnectAttempts in ${backoff}ms")
            appendLog("RECONNECT_SCHEDULED\t${System.currentTimeMillis()}\tattempt=$reconnectAttempts\tdelayMs=$backoff")

            CoroutineScope(Dispatchers.IO).launch {
                delay(backoff)
                try {
                    val request = Request.Builder().url(wsUrl).build()
                    client.newWebSocket(request, this@WebSocketListenerImpl)
                    RealTimeLogger.i("WebSocket", "Reconnect attempt $reconnectAttempts started")
                } catch (e: Exception) {
                    RealTimeLogger.w("WebSocket", "Reconnect attempt failed to start: ${e.message}")
                    appendLog("RECONNECT_FAIL\t${System.currentTimeMillis()}\t${e.message}")
                }
            }
        } catch (e: Exception) {
            RealTimeLogger.w("WebSocket", "scheduleReconnect failed: ${e.message}")
        }
    }

    private fun jsonElementToStringSafe(elem: kotlinx.serialization.json.JsonElement?): String? {
        return try {
            elem?.jsonPrimitive?.content
        } catch (_: Exception) {
            null
        }
    }

    private fun handleModelUpdate(message: String) {
        try {
            val json = Json.parseToJsonElement(message).jsonObject
            // Accept multiple possible server message type names to be robust against protocol mismatch
            val msgType = jsonElementToStringSafe(json["type"])

            // locate payload object in several possible shapes
            val payload = (json["payload"]?.jsonObject) ?: json

            // --- Handle switch_ap messages with final_assign payload ---
            try {
                val finalAssignElem = payload["final_assign"]
                if (finalAssignElem != null && finalAssignElem !is kotlinx.serialization.json.JsonNull) {
                    // extract notification_id for idempotency / ack reporting
                    val notifId = try {
                        (payload["notification_id"] ?: payload["id"] ?: payload["notificationId"])?.jsonPrimitive?.content
                    } catch (_: Exception) { null }

                    // helper: normalize notif id (fallback to timestamp if absent)
                    val notificationId = notifId ?: (payload["timestamp"]?.jsonPrimitive?.content ?: "notif_${System.currentTimeMillis()}")

                    // idempotency: check shared prefs to avoid duplicate processing
                    val prefs = context.applicationContext.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
                    val handled = prefs.getStringSet("handled_notifications", emptySet()) ?: emptySet()
                    if (handled.contains(notificationId)) {
                        RealTimeLogger.i("WebSocket", "switch_ap ignored duplicate notification_id=${notificationId}")
                        // still attempt to ACK to server
                        postNotifyAck(notificationId, "ok", "duplicate_ignored")
                        return
                    }

                    val arr = finalAssignElem.jsonArray
                    val myIdx = myTerminalIndex()
                    if (myIdx != null && myIdx >= 0 && myIdx < arr.size) {
                        val newApVal = try { arr[myIdx].jsonPrimitive.content.toIntOrNull() ?: -1 } catch (_: Exception) { -1 }
                        RealTimeLogger.i("WebSocket", "switch_ap received: myIndex=${myIdx} assigned=${newApVal}")
                        val currentAp = currentApIndex()
                        if (newApVal != currentAp && newApVal >= 0) {
                            // Measure satisfaction before switching (best-effort)
                            CoroutineScope(Dispatchers.Main).launch {
                                var satBefore: Float? = null
                                try {
                                    satBefore = try { viewModel?.measureAndLogTerminalSatisfaction(null) } catch (e: Exception) { null }
                                } catch (_: Exception) {}

                                // Decide auto-switch policy (pref key auto_switch_ap)
                                val prefs = context.applicationContext.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
                                val autoSwitch = prefs.getBoolean("auto_switch_ap", false)

                                var switched = false
                                if (autoSwitch) {
                                    try {
                                        switched = try { attemptSwitchLocal(newApVal) } catch (e: Exception) { false }
                                    } catch (_: Exception) { switched = false }
                                } else {
                                    // Open Wi‑Fi settings to let user change manually
                                    try { NetworkSwitchHelper.openWifiSettings(context) } catch (_: Exception) {}
                                    // wait a short period for user to act, then check
                                    try { kotlinx.coroutines.delay(10_000L) } catch (_: Exception) {}
                                    val nowIdx = currentApIndex()
                                    switched = nowIdx == newApVal
                                }

                                // Measure satisfaction after switching (best-effort)
                                var satAfter: Float? = null
                                try {
                                    satAfter = try { viewModel?.measureAndLogTerminalSatisfaction(null) } catch (e: Exception) { null }
                                } catch (_: Exception) {}

                                // Persist measurements to file for TelemetrySender to pick up
                                try {
                                    val jot = org.json.JSONObject()
                                    jot.put("timestamp_ms", System.currentTimeMillis())
                                    jot.putOpt("assigned_ap", newApVal)
                                    jot.putOpt("switched", switched)
                                    if (satBefore != null) jot.put("satisfaction_before", satBefore.toDouble()) else jot.putOpt("satisfaction_before", org.json.JSONObject.NULL)
                                    if (satAfter != null) jot.put("satisfaction_after", satAfter.toDouble()) else jot.putOpt("satisfaction_after", org.json.JSONObject.NULL)
                                    val f = File(context.filesDir, "terminal_satisfaction.json")
                                    f.writeText(jot.toString())
                                    RealTimeLogger.i("WebSocket", "wrote terminal_satisfaction.json switched=${switched} before=${satBefore} after=${satAfter}")
                                } catch (e: Exception) {
                                    RealTimeLogger.w("WebSocket", "failed to write terminal_satisfaction.json: ${e.message}")
                                }

                                // If server provided a model_rel in the payload, try to download/apply it and verify
                                val modelRel = try { payload["model_rel"]?.jsonPrimitive?.content } catch (_: Exception) { null }
                                var modelApplyOk = true
                                var modelApplyMsg: String? = null
                                if (!modelRel.isNullOrBlank()) {
                                    try {
                                        RealTimeLogger.i("WebSocket", "switch_ap: model_rel present=${modelRel} - attempting download and apply")
                                        // derive download base (remove trailing /download if present)
                                        val baseCandidate = if (downloadBaseUrl.endsWith("/download")) downloadBaseUrl.removeSuffix("/download") else downloadBaseUrl.trimEnd('/')
                                        val res = kotlinx.coroutines.withContext(Dispatchers.IO) {
                                            try {
                                                ModelUpdateUtil.downloadAndApplyModelSet(context, baseCandidate, client, authToken, modelRel, null)
                                            } catch (e: Exception) {
                                                RealTimeLogger.w("WebSocket", "model download/apply failed: ${e.message}")
                                                Pair(false, e.message ?: "exception")
                                            }
                                        }
                                        modelApplyOk = res.first
                                        modelApplyMsg = res.second
                                    } catch (e: Exception) {
                                        modelApplyOk = false
                                        modelApplyMsg = e.message
                                    }
                                }

                                // mark notification handled (idempotency)
                                try {
                                    val newSet = (prefs.getStringSet("handled_notifications", mutableSetOf()) ?: mutableSetOf()).toMutableSet()
                                    newSet.add(notificationId)
                                    prefs.edit().putStringSet("handled_notifications", newSet).apply()
                                } catch (e: Exception) {
                                    RealTimeLogger.w("WebSocket", "failed to mark notification handled: ${e.message}")
                                }

                                // send HTTP /notify_ack to server with status for model download/apply
                                try {
                                    if (modelApplyOk) {
                                        postNotifyAck(notificationId, "ok", "switched=${switched}")
                                    } else {
                                        postNotifyAck(notificationId, "error", "model_apply_failed:${modelApplyMsg}")
                                    }
                                } catch (e: Exception) {
                                    RealTimeLogger.w("WebSocket", "postNotifyAck failed: ${e.message}")
                                }

                                // Log outcome and ACK
                                appendLog("SWITCH_AP_HANDLED\t${System.currentTimeMillis()}\tmyIdx=${myIdx}\tassigned=${newApVal}\tswitched=${switched}")

                                // NEW: Show a notification and Toast when switch_ap handling completes
                                try {
                                    // post notification with inference result
                                    postInferenceNotification(context, null, newApVal, satAfter)

                                    // show a summary Toast (longer duration)
                                    val summaryMsg = "スイッチ完了: AP ${newApVal} に接続中 (満足度: ${satAfter?.let { String.format(Locale.getDefault(), "%.3f", it) } ?: "-"}"
                                    Toast.makeText(context, summaryMsg, Toast.LENGTH_LONG).show()
                                } catch (e: Exception) {
                                    RealTimeLogger.w("WebSocket", "Notification/Toast on switch_ap handling failed: ${e.message}")
                                }
                            }
                        } else {
                            RealTimeLogger.i("WebSocket", "switch_ap: assignment equals current AP (${currentAp}); no action")
                        }
                    } else {
                        RealTimeLogger.w("WebSocket", "switch_ap: myIndex missing or out-of-range (myIdx=${myTerminalIndex()})")
                    }
                    // ack and return — switch_ap handled
                    val roundCandidate = jsonElementToStringSafe(payload["round"]) ?: jsonElementToStringSafe(json["round"])
                    val roundVal = roundCandidate?.toIntOrNull()
                    // keep existing WS ack for compatibility
                    sendAck(roundVal)
                    return
                }
            } catch (e: Exception) {
                RealTimeLogger.w("WebSocket", "switch_ap handling failed: ${e.message}")
            }

            // Extract new keys (bin/meta)
            val modelBin = jsonElementToStringSafe(payload["model_bin"]) ?: jsonElementToStringSafe(payload["download_rel_bin"]) ?: jsonElementToStringSafe(payload["download_rel"])
            val modelMeta = jsonElementToStringSafe(payload["model_meta"]) ?: jsonElementToStringSafe(payload["download_rel_meta"])

            // Legacy fallbacks
            val downloadRel = modelBin // use bin as main rel
            val sha256 = jsonElementToStringSafe(payload["sha256"])
            val modelLink = jsonElementToStringSafe(payload["model_link"]) ?: jsonElementToStringSafe(payload["model"])

            // try to extract round number if present for ACK
            val roundCandidate = jsonElementToStringSafe(payload["round"]) ?: jsonElementToStringSafe(payload["round_number"])
            val roundVal = roundCandidate?.toIntOrNull()

            // prefer absolute modelLink when provided
            if (!modelLink.isNullOrBlank()) {
                 // Absolute URL logic — launch on IO to avoid blocking WebSocket thread
                val capturedRoundVal = roundVal
                CoroutineScope(Dispatchers.IO).launch {
                    try {
                        val (ok, msg) = ModelUpdateUtil.downloadAndApplyModelSet(
                            context, "", client, authToken, modelLink, null
                        )
                        if (!ok) {
                            if (msg == "lock") RealTimeLogger.w("WebSocket", "download skipped: already in progress") else RealTimeLogger.w("WebSocket", "download failed: $msg")
                        } else {
                            capturedRoundVal?.let {
                                persistLastAppliedRound(it)
                                viewModel?.onNewRoundReceived(it)
                            }
                        }
                    } catch (e: Exception) {
                        RealTimeLogger.e("WebSocket", "model download/apply failed: ${e.message}")
                    }
                }
                sendAck(roundVal)
                return
            }

            if (!downloadRel.isNullOrBlank()) {
                // Launch download on IO dispatcher to avoid blocking the WebSocket callback thread.
                // runBlocking here would block the WS thread, causing ping timeouts under network delay.
                val capturedRoundVal = roundVal
                CoroutineScope(Dispatchers.IO).launch {
                    try {
                        val res = ModelUpdateUtil.downloadAndApplyModelSet(context, downloadBaseUrl, client, authToken, downloadRel, modelMeta)
                        val ok = res.first
                        val msg = res.second

                        if (!ok) {
                            if (msg == "lock") RealTimeLogger.w("WebSocket", "download skipped: already in progress") else RealTimeLogger.w("WebSocket", "download failed: $msg")
                        } else {
                            capturedRoundVal?.let {
                                persistLastAppliedRound(it)
                                viewModel?.onNewRoundReceived(it)
                            }
                        }
                    } catch (e: Exception) {
                        RealTimeLogger.e("WebSocket", "model download/apply failed: ${e.message}")
                    }
                }
                sendAck(roundVal)
                return
            }

            // Fallback: Ask mode
            try {
                RealTimeLogger.i("WebSocket", "Model update notification without explicit link received; attempting controlled fetchSendToDevice (ask-mode)")
                val now = System.currentTimeMillis()
                // cooldown guard
                val last = com.example.hfl_experiment.network.FetchInFlightRegistry.lastAskTs
                val cooldown = com.example.hfl_experiment.network.FetchInFlightRegistry.askCooldownMs
                if (now - last < cooldown) {
                    appendLog("ASK_SKIPPED_COOLDOWN\t${System.currentTimeMillis()}\tlast=${last}\tcooldown=${cooldown}")
                } else if (!com.example.hfl_experiment.network.FetchInFlightRegistry.inFlight.compareAndSet(false, true)) {
                    appendLog("ASK_SKIPPED_INFLIGHT\t${System.currentTimeMillis()}")
                } else {
                    // mark attempt time immediately
                    com.example.hfl_experiment.network.FetchInFlightRegistry.lastAskTs = now
                    // launch a best-effort short-timeout fetch in background
                    CoroutineScope(Dispatchers.IO).launch {
                        val appCtx = context.applicationContext
                        val reqId = java.util.UUID.randomUUID().toString()
                        try {
                            val nc = NetworkClient(appCtx, AppConfig.getEdgeBaseUrl(appCtx), okHttpClient = client)
                            val dto = try { nc.fetchSendToDeviceShort(timeoutMs = 5000L, reqId = reqId) } catch (_: Exception) { null }
                            if (dto != null) {
                                appendLog("ASK_SUCCESS\t${System.currentTimeMillis()}\treqId=${reqId}\tdto_present=true")
                                // Extract new fields from DTO (assuming DTO has been updated or using dynamic lookup)
                                // Since SendToDeviceDto structure might not have model_bin yet, we check generic links
                                // For now, we reuse existing logic but if DTO has model_bin/model_meta we should use them.
                                // NOTE: NetworkClient.fetchSendToDevice currently returns a defined DTO class.
                                // If that class doesn't have model_bin, we might need to rely on map logic or update DTO.
                                // Assuming we fall back to 'model' link as binary.

                                val mlink = dto.links?.model ?: dto.paths?.model_rel ?: dto.model?.path
                                // val metaLink = ... (if DTO supported)

                                if (!mlink.isNullOrBlank()) {
                                    try {
                                        val dlClient = client.newBuilder()
                                            .connectTimeout(60, TimeUnit.SECONDS)
                                            .readTimeout(120, TimeUnit.SECONDS)
                                            .writeTimeout(120, TimeUnit.SECONDS)
                                            .build()

                                        var success = false
                                        // Simple binary download for now in ASK mode
                                        val res = ModelUpdateUtil.downloadAndApplyModelSet(appCtx, downloadBaseUrl, dlClient, authToken, mlink, null)
                                        success = res.first

                                        if (success) {
                                            roundVal?.let { viewModel?.onNewRoundReceived(it) } // Notify ViewModel on ASK success
                                        }

                                    } catch (e: Exception) {
                                        appendLog("ASK_DOWNLOAD_FAIL\t${System.currentTimeMillis()}\treqId=${reqId}\terr=${e.message}")
                                        RealTimeLogger.w("WebSocket", "ask-mode download failed: ${e.message}")
                                        com.example.hfl_experiment.network.FetchInFlightRegistry.recordFailure()
                                    }
                                } else {
                                    appendLog("ASK_NO_LINK_RETURNED\t${System.currentTimeMillis()}\treqId=${reqId}")
                                    com.example.hfl_experiment.network.FetchInFlightRegistry.recordFailure()
                                }
                            } else {
                                appendLog("ASK_FAIL\t${System.currentTimeMillis()}\treqId=${reqId}")
                                com.example.hfl_experiment.network.FetchInFlightRegistry.recordFailure()
                            }
                        } catch (e: Exception) {
                            appendLog("ASK_EXCEPTION\t${System.currentTimeMillis()}\treqId=${reqId}\terr=${e.message}")
                            com.example.hfl_experiment.network.FetchInFlightRegistry.recordFailure()
                            RealTimeLogger.w("WebSocket", "ask-mode exception: ${e.message}")
                        } finally {
                            com.example.hfl_experiment.network.FetchInFlightRegistry.inFlight.set(false)
                        }
                    }
                }
            } catch (_: Exception) {
                RealTimeLogger.w("WebSocket", "Error handling fallback fetchSendToDevice")
            }

              // always attempt to ACK the notification even if we only did a fetch
              sendAck(roundVal)

         } catch (e: Exception) {
             RealTimeLogger.e("WebSocket", "Error handling model update: ${e.message}", e)
             appendLog("ERR\t${System.currentTimeMillis()}\t${e.message}")
         }
     }

     private fun sendAck(round: Int?) {
         try {
             // send ACK asynchronously; prefer authoritative round from server /meta when available
             CoroutineScope(Dispatchers.IO).launch {
                 try {
                     val prefs = context.applicationContext.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
                     val storedRound = if (prefs.contains("last_applied_round")) prefs.getInt("last_applied_round", -1).let { if (it >= 0) it else null } else null
                     var ackRound: Int? = round ?: storedRound

                     // Try to fetch authoritative round from server meta (best-effort)
                     try {
                         val nc = NetworkClient(context, AppConfig.getEdgeBaseUrl(context), okHttpClient = client)
                         val meta = try { nc.fetchMeta(forceRefresh = true) } catch (_: Exception) { null }
                         if (meta != null) ackRound = meta.round
                     } catch (_: Exception) {
                         // ignore - fall back to provided/stored round
                     }

                     val ackJson = if (ackRound != null) {
                         "{\"type\":\"ack\",\"ack_type\":\"round_update\",\"round\":$ackRound}"
                     } else {
                         "{\"type\":\"ack\",\"ack_type\":\"round_update\"}"
                     }

                     val sent = try { webSocketRef?.send(ackJson) ?: false } catch (_: Exception) { false }
                     if (sent) {
                         appendLog("ACK_SENT\t${System.currentTimeMillis()}\t$ackJson")
                         RealTimeLogger.i("WebSocket", "ACK sent: $ackJson")
                     } else {
                         appendLog("ACK_SEND_FAILED\t${System.currentTimeMillis()}\t$ackJson")
                         RealTimeLogger.w("WebSocket", "Failed to send ACK (socket null or send failed): $ackJson")
                     }
                 } catch (e: Exception) {
                     RealTimeLogger.w("WebSocket", "sendAck coroutine failed: ${e.message}")
                 }
             }
         } catch (e: Exception) {
             RealTimeLogger.w("WebSocket", "sendAck failed: ${e.message}")
         }
     }

    // persist last applied round to stable storage so we can reference it across reconnects
    private fun persistLastAppliedRound(round: Int) {
        try {
            val prefs = context.applicationContext.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
            prefs.edit().putInt("last_applied_round", round).apply()
            appendLog("ROUND_STORED\t${System.currentTimeMillis()}\tround=$round")
        } catch (e: Exception) {
            RealTimeLogger.w("WebSocket", "persistLastAppliedRound failed: ${e.message}")
        }
    }

    private fun appendLog(line: String) {
        try {
            val logF = File(context.filesDir, "model_update_log.txt")
            logF.appendText(line + "\n")
        } catch (_: Exception) {
            // best-effort only; don't crash if logging fails
            RealTimeLogger.w("WebSocket", "failed to append log")
        }
    }

    // helper to post HTTP /notify_ack to server
    private fun postNotifyAck(notificationId: String, status: String, message: String? = null) {
        try {
            CoroutineScope(Dispatchers.IO).launch {
                try {
                    val url = "${AppConfig.getEdgeBaseUrl(context)}/notify_ack"
                    val jsonBody = org.json.JSONObject()
                    jsonBody.put("notification_id", notificationId)
                    jsonBody.put("status", status)
                    message?.let { jsonBody.put("message", it) }

                    val body = jsonBody.toString().toRequestBody("application/json".toMediaTypeOrNull())
                    val request = Request.Builder()
                        .url(url)
                        .post(body)
                        .build()

                    val response = client.newCall(request).execute()
                    if (!response.isSuccessful) {
                        RealTimeLogger.w("WebSocket", "postNotifyAck failed: HTTP ${response.code} ${response.message}")
                    } else {
                        RealTimeLogger.i("WebSocket", "postNotifyAck success: $notificationId")
                    }
                } catch (e: Exception) {
                    RealTimeLogger.w("WebSocket", "postNotifyAck exception: ${e.message}")
                }
            }
        } catch (e: Exception) {
            RealTimeLogger.w("WebSocket", "postNotifyAck setup failed: ${e.message}")
        }
    }

    // Helper: ensure notification channel exists (id: HFL_INFERENCE_CHANNEL)
    private fun ensureInferenceNotificationChannel(ctx: Context) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
                val id = "HFL_INFERENCE_CHANNEL"
                if (nm.getNotificationChannel(id) == null) {
                    val name = "Inference Notifications"
                    val desc = "Notifications when server inference suggests AP switches"
                    val chan = NotificationChannel(id, name, NotificationManager.IMPORTANCE_DEFAULT)
                    chan.description = desc
                    nm.createNotificationChannel(chan)
                }
            }
        } catch (e: Exception) {
            RealTimeLogger.w("WebSocket", "ensureInferenceNotificationChannel failed: ${e.message}")
        }
    }

    // Helper: post a notification summarizing the inference/switch outcome
    private fun postInferenceNotification(ctx: Context, round: Int?, apId: Int, sat: Float?) {
        try {
            // Check runtime permission on Android 13+
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                val perm = Manifest.permission.POST_NOTIFICATIONS
                if (ctx.checkSelfPermission(perm) != PackageManager.PERMISSION_GRANTED) {
                    // Permission not granted — cannot post a system notification. Log and show a toast instructing the user.
                    try { Toast.makeText(ctx, "通知権限が必要です: 設定から POST_NOTIFICATIONS を許可してください", Toast.LENGTH_LONG).show() } catch (_: Exception) {}
                    RealTimeLogger.w("WebSocket", "postInferenceNotification: missing POST_NOTIFICATIONS permission")
                    return
                }
            }

            ensureInferenceNotificationChannel(ctx)
            val channelId = "HFL_INFERENCE_CHANNEL"

            val title = if (round != null) "Round ${round}: AP ${apId} に接続中" else "AP ${apId} に接続中"
            val body = "満足度: ${if (sat != null) String.format(Locale.getDefault(), "%.3f", sat) else "-"}"

            val intent = Intent(ctx, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            }
            val pending = PendingIntent.getActivity(ctx, (System.currentTimeMillis() % Int.MAX_VALUE).toInt(), intent, PendingIntent.FLAG_UPDATE_CURRENT or if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0)

            val builder = NotificationCompat.Builder(ctx, channelId)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle(title)
                .setContentText(body)
                .setContentIntent(pending)
                .setAutoCancel(true)

            val nid = (System.currentTimeMillis() and 0xfffffff).toInt()
            with(NotificationManagerCompat.from(ctx)) { notify(nid, builder.build()) }
        } catch (e: Exception) {
            RealTimeLogger.w("WebSocket", "postInferenceNotification failed: ${e.message}")
        }
    }
}

// 改善: OkHttpClient を外部から渡すようにして、newWebSocket直後に executor を shutdown しない
fun startWebSocket(wsUrl: String, downloadBaseUrl: String, context: Context, client: OkHttpClient, authToken: String?, viewModel: TrainingViewModel?): WebSocketListenerImpl? {
    return try {
        val request = Request.Builder().url(wsUrl).build()
        val listener = WebSocketListenerImpl(wsUrl, downloadBaseUrl, context, client, authToken, viewModel)
        client.newWebSocket(request, listener)
        // DO NOT shutdown the client's dispatcher here — that will immediately stop the WebSocket background threads
        RealTimeLogger.d("WebSocket", "WebSocket start requested: $wsUrl")
        listener
    } catch (e: Exception) {
        RealTimeLogger.e("WebSocket", "Failed to start WebSocket: ${e.message}", e)
        null
    }
}

class MainActivity : ComponentActivity() {
    private lateinit var networkClientInstance: NetworkClient
    // Track the active WebSocket listener so we can stop it before reconnecting
    private var currentWsListener: WebSocketListenerImpl? = null
    // keep a dedicated OkHttpClient instance for websocket lifetime
    private val wsClient: OkHttpClient by lazy {
        // OkHttpClient.Builder()
        //    .pingInterval(20, TimeUnit.SECONDS)
        //    .retryOnConnectionFailure(true)
        //    .connectTimeout(30, TimeUnit.SECONDS)
        //    // keep read timeout disabled so websocket can stay open indefinitely
        //    .readTimeout(0, TimeUnit.MILLISECONDS)
        //    .writeTimeout(30, TimeUnit.SECONDS)
        //    .build()

        // Improved: add HttpLoggingInterceptor in DEBUG builds so the HTTP-level WebSocket handshake
        // (Upgrade: websocket / Sec-WebSocket-Accept / HTTP/1.1 101) will be visible in logcat.
        val builder = OkHttpClient.Builder()
            .pingInterval(20, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .connectTimeout(30, TimeUnit.SECONDS)
            // keep read timeout disabled so websocket can stay open indefinitely
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)

        try {
            if (BuildConfig.DEBUG) {
                val logging = okhttp3.logging.HttpLoggingInterceptor { msg -> RealTimeLogger.d("OkHttp", msg) }
                logging.level = okhttp3.logging.HttpLoggingInterceptor.Level.HEADERS
                builder.addInterceptor(logging)
            }
        } catch (e: Exception) {
            RealTimeLogger.w("MainActivity", "Failed to add HttpLoggingInterceptor: ${e.message}")
        }

        builder.build()
    }
    // guard: use process-wide registry so manual fetch and ask-mode share the same in-flight flag
    // manualFetchLock (legacy) kept for compatibility with other code that may reference it
    // but prefer FetchInFlightRegistry.inFlight for enforcement
    // private val manualFetchLock = AtomicBoolean(false)
    private lateinit var trainingViewModel: TrainingViewModel
    // modelUpdateReceiver will be created and registered in onCreate after trainingViewModel is initialized.

    @SuppressLint("UnprotectedBroadcastReceiver")
    private fun ensureWriteSettingsPermission(onGranted: () -> Unit) {
        try {
            if (Settings.System.canWrite(this)) {
                onGranted()
                return
            }
            Toast.makeText(this, "システム設定の変更権限が必要です。許可してください。", Toast.LENGTH_SHORT).show()
            val intent = Intent(Settings.ACTION_MANAGE_WRITE_SETTINGS).apply {
                data = Uri.parse("package:${packageName}")
            }
            startActivity(intent)
            // best-effort recheck after short delay
            lifecycleScope.launch {
                delay(1000)
                if (Settings.System.canWrite(this@MainActivity)) onGranted()
            }
        } catch (e: Exception) {
            RealTimeLogger.w("MainActivity", "ensureWriteSettingsPermission failed: ${e.message}")
        }
    }

    @SuppressLint("UnprotectedBroadcastReceiver")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        // 毎回起動時に接続先設定画面を表示（ユーザが確認・変更できるよう��する）
        // 設定画面で「保存」を押すと MainActivity に戻る
        if (!intent.getBooleanExtra("from_setup", false)) {
            startActivity(Intent(this, EdgeEndpointSetupActivity::class.java))
            finish()
            return
        }

        AppConfig.migrateTerminalIdentityConfiguredFlagIfNeeded(this)
        if (!AppConfig.isTerminalIdentityConfigured(this)) {
            startActivity(Intent(this, TerminalIdentitySetupActivity::class.java))
            finish()
            return
        }

        // Load device-specific config from assets (if present) to override defaults
        AppConfig.loadDeviceConfig(this)

        // Ensure current_ap_index is initialized: detect connected SSID and write index (0 or 1); fallback to 0
        try {
            val prefs = getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
            val cur = prefs.getInt("current_ap_index", -1)
            if (cur == -1) {
                try {
                    val ssid = com.example.hfl_experiment.util.networking.NetworkSwitchHelper.getConnectedSsid(this)
                    val a = AppConfig.getPrimarySsid(this)
                    val b = AppConfig.getSecondarySsid(this)
                    val idx = when {
                        ssid == null -> 0
                        ssid == a -> 0
                        ssid == b -> 1
                        else -> 0
                    }
                    prefs.edit().putInt("current_ap_index", idx).apply()
                    RealTimeLogger.i("MainActivity", "Initialized current_ap_index from SSID='$ssid' -> idx=$idx")
                } catch (e: Exception) {
                    try { getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE).edit().putInt("current_ap_index", 0).apply() } catch (_: Exception) {}
                    RealTimeLogger.w("MainActivity", "Failed to detect SSID for current_ap_index init: ${e.message}; defaulted to 0")
                }
            } else {
                RealTimeLogger.i("MainActivity", "current_ap_index already set in prefs: ${cur}")
            }
        } catch (e: Exception) {
            RealTimeLogger.w("MainActivity", "current_ap_index init block failed: ${e.message}")
        }

        // Initialize realtime logger early
        try { com.example.hfl_experiment.util.logging.RealTimeLogger.init(this) } catch (_: Exception) {}

        // Ensure WRITE_SETTINGS permission (system settings modification) for network switching helpers
        ensureWriteSettingsPermission {
            RealTimeLogger.i("MainActivity", "WRITE_SETTINGS permission confirmed")
        }

        // Initialize NetworkClientInstance with BuildConfig values
        val runtimeBase = AppConfig.getEdgeBaseUrl(this)
        val runtimeToken = AppConfig.getServerAuthToken(this)
        networkClientInstance = NetworkClient(
            context = this,
            baseUrl = runtimeBase,
            okHttpClient = OkHttpClient(),
            authToken = runtimeToken
        )

        // NOTE: replace placeholders with real host values in production. Use secure token from AppConfig.
        // Build download & websocket URLs from configured EDGE_BASE_URL (runtime prefs override)
        val downloadBaseUrl = AppConfig.getDownloadBaseUrl(this)
        val wsUrl = AppConfig.getWebSocketUrl(this, runtimeToken)

        // 既存ログ出力を更新
        RealTimeLogger.i("MainActivity", "Using downloadBaseUrl=$downloadBaseUrl wsUrl=$wsUrl")
        // obtain ViewModel for activity-level callbacks
        trainingViewModel = ViewModelProvider(this)[TrainingViewModel::class.java]
        // register model update receiver now that trainingViewModel is available
        try {
            val receiver = object : BroadcastReceiver() {
                override fun onReceive(context: Context?, intent: Intent?) {
                    val path = intent?.getStringExtra("path")
                    try {
                        trainingViewModel.notifyExternalModelUpdate(path)
                    } catch (e: Exception) {
                        RealTimeLogger.w("MainActivity", "modelUpdateReceiver handling failed: ${e.message}")
                    }
                }
            }
            registerReceiverNotExported(this, receiver, IntentFilter(ModelUpdateUtil.ACTION_MODEL_UPDATED))
        } catch (e: Exception) {
            RealTimeLogger.w("MainActivity", "failed to register modelUpdateReceiver via compat helper: ${e.message}")
        }

        // start websocket using the persistent wsClient (do not shutdown its executor)
        RealTimeLogger.i("MainActivity", "Using downloadBaseUrl=$downloadBaseUrl wsUrl=$wsUrl")
        currentWsListener = startWebSocket(wsUrl, downloadBaseUrl, this, wsClient, runtimeToken, trainingViewModel)

        // --- load app.json from a few candidate locations (filesDir, external files, assets) ---
        val appConfigs = loadAppTypesSafe(this)

        setContent {
            HFL_experimentTheme {
                val vm: TrainingViewModel = trainingViewModel
                val trainingProgress by vm.trainingProgress.collectAsStateWithLifecycle()
                val trainingFinished by vm.trainingFinished.collectAsStateWithLifecycle()
                val isUploading by vm.isUploading.collectAsStateWithLifecycle()
                val isTrainingRunning by vm.isTrainingRunning.collectAsStateWithLifecycle()
                val errorMessage by vm.errorMessage.collectAsStateWithLifecycle()
                val trainingDataStatus by vm.trainingDataStatus.collectAsStateWithLifecycle()
                val isTrainingDataReady by vm.isTrainingDataReady.collectAsStateWithLifecycle()
                val pollMaxAttempts by vm.pollMaxAttempts.collectAsStateWithLifecycle()
                val pollDelayMs by vm.pollDelayMs.collectAsStateWithLifecycle()
                val currentPollAttempt by vm.currentPollAttempt.collectAsStateWithLifecycle()
                val currentCycle by vm.currentCycle.collectAsStateWithLifecycle()
                val totalCycles by vm.totalCycles.collectAsStateWithLifecycle()
                val currentEpoch by vm.currentEpoch.collectAsStateWithLifecycle()
                val epochsPerCycle by vm.epochsPerCycle.collectAsStateWithLifecycle()
                val networkPanelRequested by vm.networkPanelRequested.collectAsStateWithLifecycle()
                val networkSuggestionMsg by vm.networkSuggestionMsg.collectAsStateWithLifecycle()
                // NEW: Collect log upload status from ViewModel
                val logUploadStatus by vm.logUploadStatus.collectAsStateWithLifecycle()

                // pass configs to ViewModel once
                LaunchedEffect(appConfigs) {
                    appConfigs?.let { vm.setAppConfigs(it) }
                }

                // Observe terminal satisfaction, current AP and server round from ViewModel
                val terminalSatisfaction by vm.terminalSatisfaction.collectAsStateWithLifecycle()
                val currentAp by vm.currentApIndex.collectAsStateWithLifecycle()
                val serverRoundVal by vm.serverRound.collectAsStateWithLifecycle()

                // Listen for inference events and show a Toast when received
                LaunchedEffect(vm) {
                    try {
                        vm.inferenceEvent.collect { msg ->
                            Toast.makeText(this@MainActivity, "📡 Server Inference Received! ($msg)", Toast.LENGTH_LONG).show()
                        }
                    } catch (_: Exception) {}
                }

                 // pass configs to ViewModel once
                 LaunchedEffect(appConfigs) {
                     appConfigs?.let { vm.setAppConfigs(it) }
                 }

                 // Observe network forced event from ViewModel and trigger suggestion/panel display
                 val networkForced by vm.networkForcedEvent.collectAsStateWithLifecycle()
                 LaunchedEffect(networkForced) {
                    val target = networkForced
                    if (!target.isNullOrBlank()) {
                        try {
                            val ctx = this@MainActivity
                            // 修正：自動切り替えを削除し、常に設定パネルを開くように変更
                            NetworkSwitchHelper.openWifiSettings(ctx)
                            Toast.makeText(ctx, "ネットワーク切替が必要です: $target", Toast.LENGTH_LONG).show()
                            // clear event
                            vm.clearNetworkForcedEvent()
                        } catch (e: Exception) {
                            RealTimeLogger.w("MainActivity", "network switch handling failed: ${e.message}")
                        }
                    }
                }

                val rttValue = remember { mutableStateOf<Long?>(null) }
                // ViewModel StateFlow から満足度を直接受け取る（手動計測・自動計測どちらも反映される）
                val satisfactionBeforeState by vm.satisfactionBefore.collectAsStateWithLifecycle()
                val satisfactionAfterState by vm.satisfactionAfter.collectAsStateWithLifecycle()

                // --- 起動チェック ---
                // サーバ接続・トークン・モデル・アプリ設定を起動時に一度だけ確認する
                data class StartupCheckResult(val ok: Boolean, val label: String, val detail: String = "")
                val startupChecks = remember { mutableStateOf<List<StartupCheckResult>?>(null) }
                val startupCheckRunning = remember { mutableStateOf(true) }
                LaunchedEffect(Unit) {
                    val results = mutableListOf<StartupCheckResult>()
                    // 1. 認証トークン設定済みか
                    val token = com.example.hfl_experiment.AppConfig.getServerAuthToken(applicationContext)
                    results.add(StartupCheckResult(!token.isNullOrBlank(), "認証トークン", if (token.isNullOrBlank()) "未設定（SettingsActivityで設定）" else "設定済み"))
                    // 2. サーバ接続・モデルラウンド確認
                    try {
                        val meta = withContext(kotlinx.coroutines.Dispatchers.IO) {
                            networkClientInstance.fetchMeta(forceRefresh = true)
                        }
                        results.add(StartupCheckResult(true, "サーバ接続", "接続OK (round=${meta.round})"))
                        results.add(StartupCheckResult(meta.round > 0, "モデル配信", if (meta.round > 0) "round=${meta.round}" else "round=0（モデル未配信の可能性）"))
                    } catch (e: Exception) {
                        results.add(StartupCheckResult(false, "サーバ接続", "失敗: ${e.message?.take(40)}"))
                        results.add(StartupCheckResult(false, "モデル配信", "サーバ未接続のため確認不可"))
                    }
                    // 3. アプリ設定読み込み済みか
                    results.add(StartupCheckResult(!appConfigs.isNullOrEmpty(), "app.json", if (appConfigs.isNullOrEmpty()) "未読み込み" else "${appConfigs!!.size}種類読み込み済み"))
                    startupChecks.value = results
                    startupCheckRunning.value = false
                }

                // Composeのイベントハンドラ内でコルーチンを起動するためのスコープ
                val scope = rememberCoroutineScope()
                // 全体のスクロール状態（画面が小さいときに下のボタンが隠れないように）
                val scrollState = rememberScrollState()

                Scaffold { paddingValues ->
                    Column(modifier = Modifier.padding(paddingValues).verticalScroll(scrollState)) {

                        // --- 起動チェックカード ---
                        Card(
                            modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 4.dp),
                            elevation = CardDefaults.cardElevation(defaultElevation = 2.dp)
                        ) {
                            Column(modifier = Modifier.padding(10.dp)) {
                                Text("起動チェック", style = MaterialTheme.typography.labelLarge)
                                Spacer(Modifier.height(4.dp))
                                if (startupCheckRunning.value) {
                                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp)
                                        Text("確認中...", style = MaterialTheme.typography.bodySmall)
                                    }
                                } else {
                                    startupChecks.value?.forEach { check ->
                                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                            Text(if (check.ok) "✅" else "❌", style = MaterialTheme.typography.bodySmall)
                                            Text("${check.label}: ${check.detail}", style = MaterialTheme.typography.bodySmall,
                                                color = if (check.ok) androidx.compose.ui.graphics.Color(0xFF2E7D32) else androidx.compose.ui.graphics.Color(0xFFD32F2F))
                                        }
                                    }
                                }
                            }
                        }

                        // --- Status Card: Satisfaction / Current AP / Server Round ---
                        Card(modifier = Modifier
                            .fillMaxWidth()
                            .padding(bottom = 8.dp), elevation = CardDefaults.cardElevation(defaultElevation = 4.dp)) {
                            Column(modifier = Modifier.padding(12.dp)) {
                                val sat = terminalSatisfaction ?: Float.NaN
                                val satText = if (sat.isNaN()) "n/a" else "${"%.3f".format(sat)}"
                                val satColor = when {
                                    !sat.isNaN() && sat > 0.8f -> androidx.compose.ui.graphics.Color(0xFF2E7D32)
                                    !sat.isNaN() && sat < 0.5f -> androidx.compose.ui.graphics.Color(0xFFD32F2F)
                                    else -> androidx.compose.ui.graphics.Color(0xFFBDBDBD)
                                }
                                Text(text = "Satisfaction: $satText", style = MaterialTheme.typography.titleLarge, color = satColor)
                                LinearProgressIndicator(progress = (terminalSatisfaction ?: 0f).coerceIn(0f,1f), modifier = Modifier.fillMaxWidth().height(8.dp))
                                Spacer(modifier = Modifier.height(6.dp))
                                Row(horizontalArrangement = Arrangement.SpaceBetween, modifier = Modifier.fillMaxWidth()) {
                                    Text("Current AP: $currentAp", style = MaterialTheme.typography.bodyMedium)
                                    Text("Server Round: ${serverRoundVal ?: "-"}", style = MaterialTheme.typography.bodyMedium)
                                }
                            }
                        }

                        TrainingScreen(
                            // イベントハンドラ
                            onMeasureRtt = {
                                // measure RTT via NetworkClient (OkHttp head requests) and store into rttValue
                                lifecycleScope.launch {
                                    try {
                                        val rttF = withContext(kotlinx.coroutines.Dispatchers.IO) {
                                            try {
                                                // ask NetworkClient to measure RTT to baseUrl
                                                networkClientInstance.measureRtt(null, attempts = 3, perAttemptTimeoutMs = 3000)
                                            } catch (e: Exception) {
                                                RealTimeLogger.w("MainActivity", "networkClient.measureRtt failed: ${e.message}")
                                                null
                                            }
                                        }
                                        if (rttF != null) {
                                            rttValue.value = rttF.toLong()
                                        } else {
                                            // fallback to local ping util for gateway RTT
                                            val gw = withContext(kotlinx.coroutines.Dispatchers.IO) { com.example.hfl_experiment.util.networking.PingUtil.pingGateway(applicationContext) }
                                            rttValue.value = gw?.toLong()
                                        }
                                    } catch (e: Exception) {
                                        RealTimeLogger.w("MainActivity", "onMeasureRtt handler failed: ${e.message}")
                                    }
                                }
                            },
                            onFetchTrainingData = { selectedApps -> trainingViewModel.fetchAndPrepareTrainingData(selectedApps) },
                            // Pass the one-time assigned app index so UI default selection matches ViewModel
                            assignedAppIndex = vm.assignedAppIndex.collectAsStateWithLifecycle().value,
                            // NEW: measure satisfaction callbacks (Before / After). Keep generic onMeasureSatisfaction=null to avoid double-invoke
                            onMeasureSatisfaction = null,
                            onMeasureSatisfactionBefore = { selectedApps ->
                                lifecycleScope.launch {
                                    // label="before" を明示して記録
                                    val s = try { trainingViewModel.measureAndLogTerminalSatisfaction(selectedApps, label = "before") } catch (e: Exception) { null }
                                    if (s != null) {
                                        android.widget.Toast.makeText(this@MainActivity, "満足度(Before)= ${"%.3f".format(s)}", android.widget.Toast.LENGTH_LONG).show()
                                    } else {
                                        android.widget.Toast.makeText(this@MainActivity, "満足度(Before)計測失敗", android.widget.Toast.LENGTH_LONG).show()
                                    }
                                }
                            },
                            onMeasureSatisfactionAfter = { _ ->
                                lifecycleScope.launch {
                                    // measureSatisfactionAfter() はセッションIDに紐づいた after 計測＋experiment record 更新を行う
                                    val s = try { trainingViewModel.measureSatisfactionAfter() } catch (e: Exception) { null }
                                    if (s != null) {
                                        android.widget.Toast.makeText(this@MainActivity, "満足度(After)= ${"%.3f".format(s)}", android.widget.Toast.LENGTH_LONG).show()
                                    } else {
                                        android.widget.Toast.makeText(this@MainActivity, "満足度(After)計測失敗", android.widget.Toast.LENGTH_LONG).show()
                                    }
                                }
                            },

                            // translate start request into ViewModel actions
                            onStartTraining = { selectedApps, autoUpload ->
                                // Prevent duplicate starts and provide feedback
                                if (isTrainingRunning) {
                                    Toast.makeText(this@MainActivity, "学習は既に実行中です。しばらくお待ちください。", Toast.LENGTH_SHORT).show()
                                } else if (trainingFinished) {
                                    Toast.makeText(this@MainActivity, "学習は完了しています。新しいラウンドを待つか、デバッグでフラグをリセットしてください。", Toast.LENGTH_LONG).show()
                                } else {
                                    val tpRttFeatures = 2
                                    val appNumMax = appConfigs?.size ?: AppConfig.APP_CAT_COUNT
                                    // Start the suspend runFiveCycles in a coroutine (5 cycles, each with epochs=5)
                                    lifecycleScope.launch {
                                        try {
                                            // Force autoUpload=true as per requirement: "学習完了時には自動でアップロードは必須にしてほしい"
                                            trainingViewModel.runFiveCycles(
                                                inputSize = tpRttFeatures + appNumMax,
                                                hiddenSize = 32,
                                                outputSize = appNumMax,
                                                epochs = 5,
                                                autoUpload = true, // Forced true
                                            )
                                        } catch (e: kotlinx.coroutines.CancellationException) {
                                            throw e
                                        } catch (e: Exception) {
                                            RealTimeLogger.w("MainActivity", "runFiveCycles failed: ${e.message}")
                                        }
                                    }
                                }
                            },
                            onUploadBiases = { trainingViewModel.uploadUpdate() },
                            onCancelUpload = { trainingViewModel.cancelUpload() },
                            onRetryUpload = null,
                            uploadProgress = 0f,
                            uploadAttempt = 0,
                            onCancelRun = { trainingViewModel.cancelRunFiveCycles() },
                            onFullReset = {
                                trainingViewModel.softReset()
                                // 旧リスナーを停止してからWebSocket再接続（ゾンビ防止）
                                try {
                                    currentWsListener?.stop()
                                    currentWsListener = null
                                    val runtimeToken = AppConfig.getServerAuthToken(this@MainActivity)
                                    val downloadBaseUrl = AppConfig.getDownloadBaseUrl(this@MainActivity)
                                    val wsUrl = AppConfig.getWebSocketUrl(this@MainActivity, runtimeToken)
                                    currentWsListener = startWebSocket(wsUrl, downloadBaseUrl, this@MainActivity, wsClient, runtimeToken, trainingViewModel)
                                    RealTimeLogger.i("MainActivity", "onFullReset: WebSocket reconnected after soft reset")
                                } catch (e: Exception) {
                                    RealTimeLogger.w("MainActivity", "onFullReset: WebSocket reconnect failed: ${e.message}")
                                }
                            },
                            onSetPollParams = { a, d -> trainingViewModel.setPollParams(a, d) },
                            onClearSavedRound = { trainingViewModel.clearSavedRound() },
                            onResetTrainingFinished = { trainingViewModel.resetTrainingFinishedFlag() },
                            // Pass the lambda to trigger the forced switch from UI
                            onForcedSwitch = {
                                scope.launch {
                                    try {
                                        val ctx = this@MainActivity
                                        // 修正：自動切り替えを削除し、常に設定パネルを開くように変更
                                        NetworkSwitchHelper.openWifiSettings(ctx)
                                        Toast.makeText(ctx, "ネットワーク設定パネルを開きました。手動で切り替えてください。", Toast.LENGTH_LONG).show()
                                    } catch (e: Exception) {
                                        RealTimeLogger.w("MainActivity", "Forced switch failed: ${e.message}")
                                        Toast.makeText(this@MainActivity, "ネットワーク設定を開けませんでした: ${e.message}", Toast.LENGTH_SHORT).show()
                                    }
                                }
                            },
                            // NEW: Pass log upload functions and state
                            onUploadLogFile = { vm.uploadCurrentLogFile() },
                            logUploadStatus = logUploadStatus,
                            onClearLogUploadStatus = { vm.clearLogUploadStatus() },
                            isLogUploading = vm.isLogUploading.collectAsState().value,

                            // 状態パラメータ
                            rttValue = rttValue.value,
                            trainingProgress = trainingProgress,
                            trainingFinished = trainingFinished,
                            isUploadingBiases = isUploading,
                            errorMessage = errorMessage,
                            trainingDataStatus = trainingDataStatus,
                            isTrainingDataReady = isTrainingDataReady,
                            isTrainingRunning = isTrainingRunning,
                            pollMaxAttempts = pollMaxAttempts,
                            pollDelayMs = pollDelayMs,
                            currentPollAttempt = currentPollAttempt,
                            availableApps = appConfigs?.map { it.appType },
                            initialAutoUpload = false,
                            currentCycle = currentCycle,
                            totalCycles = totalCycles,
                            currentEpoch = currentEpoch,
                            epochsPerCycle = epochsPerCycle,
                            satisfactionBefore = satisfactionBeforeState,
                            satisfactionAfter = satisfactionAfterState,
                            modifier = Modifier.padding(horizontal = 16.dp)
                        )

                        // ▼▼▼ ここが修正箇所 ▼▼▼
                        // Jetpack ComposeのButtonコンポーザブルを使ってボタンを作成
                        Button(
                            onClick = {
                                scope.launch {
                                    try {
                                        val openApiSchema = networkClientInstance.fetchOpenApiSchema()
                                        val isValid = networkClientInstance.validateDtoConsistency(openApiSchema, SampleDto())
                                        val comparisonResult = if (isValid) {
                                            "DTO is consistent with OpenAPI schema."
                                        } else {
                                            "DTO is inconsistent with OpenAPI schema."
                                        }

                                        // ResultActivityに結果を渡して画面遷移
                                        val intent = Intent(this@MainActivity, ResultActivity::class.java).apply {
                                            putExtra("comparison_result", comparisonResult)
                                        }
                                        startActivity(intent)
                                    } catch (e: Exception) {
                                        Toast.makeText(this@MainActivity, "Validation failed: ${e.message}", Toast.LENGTH_LONG).show()
                                    }
                                }
                            },
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 8.dp)
                        ) {
                            Text("Validate DTO")
                        }

                        // Upload collected training metrics to server
                        Button(
                            onClick = {
                                // Enqueue a WorkManager job to perform upload in background (retries/constraints handled by WorkManager)
                                val request = OneTimeWorkRequestBuilder<UploadMetricsWorker>().build()
                                WorkManager.getInstance(this@MainActivity).enqueue(request)

                                // Observe work completion to show a Toast when finished
                                WorkManager.getInstance(this@MainActivity).getWorkInfoByIdLiveData(request.id)
                                    .observe(this@MainActivity) { info: WorkInfo? ->
                                        info?.let { wi ->
                                            if (wi.state.isFinished) {
                                                val ok = wi.state == WorkInfo.State.SUCCEEDED
                                                val msg = if (ok) "Metrics uploaded (worker success)" else "Metrics upload worker finished (retry or failed)"
                                                Toast.makeText(this@MainActivity, msg, Toast.LENGTH_LONG).show()
                                            }
                                        }
                                    }
                            },
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 8.dp)
                        ) {
                            Text("Upload Metrics")
                        }

                        // 新規: ラウンドリセットボタン
                        Button(
                            onClick = {
                                scope.launch {
                                    try {
                                        val ok = networkClientInstance.resetRound()
                                        if (ok) {
                                            Toast.makeText(this@MainActivity, "Round reset successfully.", Toast.LENGTH_LONG).show()
                                        } else {
                                            Toast.makeText(this@MainActivity, "Round reset failed.", Toast.LENGTH_LONG).show()
                                        }
                                    } catch (e: Exception) {
                                        Toast.makeText(this@MainActivity, "Round reset error: ${e.message}", Toast.LENGTH_LONG).show()
                                    }
                                }
                            },
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 8.dp)
                        ) {
                            Text("Reset Round")
                        }

                        // Manual: Fetch New Model (anytime) — will attempt to download/apply model immediately
                        // NEW: Dump and auto-upload collected logs to edge (manual trigger)
                        Button(
                            onClick = {
                                scope.launch {
                                    try {
                                        val ok = trainingViewModel.dumpAndUploadLogs()
                                        if (ok) Toast.makeText(this@MainActivity, "Logs uploaded successfully (or uploaded immediately)", Toast.LENGTH_LONG).show()
                                        else Toast.makeText(this@MainActivity, "Logs upload scheduled (worker) or failed", Toast.LENGTH_LONG).show()
                                    } catch (e: Exception) {
                                        Toast.makeText(this@MainActivity, "Logs upload failed: ${e.message}", Toast.LENGTH_LONG).show()
                                    }
                                }
                            },
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 8.dp),
                            enabled = !trainingViewModel.isLogUploading.collectAsState().value
                        ) {
                            Text("Dump & Upload Logs")
                        }

                         Button(
                             onClick = {
                                 scope.launch {
                                     if (!com.example.hfl_experiment.network.FetchInFlightRegistry.inFlight.compareAndSet(false, true)) {
                                         Toast.makeText(this@MainActivity, "既に手動取得中です。", Toast.LENGTH_SHORT).show()
                                         return@launch
                                     }
                                     try {
                                         Toast.makeText(this@MainActivity, "モデル取得を開始します...", Toast.LENGTH_SHORT).show()
                                         val dto = try {
                                             withContext(Dispatchers.IO) { networkClientInstance.fetchSendToDevice() }
                                         } catch (e: Exception) {
                                             Toast.makeText(this@MainActivity, "取得失敗: ${e.message}", Toast.LENGTH_LONG).show()
                                             RealTimeLogger.e("MainActivity", "fetchSendToDevice failed: ${e.message}", e)
                                             return@launch
                                         }

                                        // Prefer links.model (absolute URL) if present, otherwise use paths.model_rel
                                        // Update to support model_bin/model_meta in DTO if available
                                        val modelLink = dto.links?.model ?: dto.model?.path
                                        if (modelLink.isNullOrBlank()) {
                                            Toast.makeText(this@MainActivity, "サーバがモデル情報を返しませんでした。", Toast.LENGTH_LONG).show()
                                            RealTimeLogger.w("MainActivity", "sendToDevice returned no model link/path: $dto")
                                            return@launch
                                        }

                                        val (ok, msg) = if (modelLink.startsWith("http://") || modelLink.startsWith("https://")) {
                                             // absolute URL — remove extra null arg to match signature (expectedSha256)
                                             withContext(Dispatchers.IO) {
                                                 ModelUpdateUtil.downloadAndApplyModelFromUrlWithLock(
                                                     this@MainActivity,
                                                     modelLink,
                                                     wsClient,
                                                     AppConfig.getServerAuthToken(this@MainActivity),
                                                     null
                                                 )
                                             }
                                         } else {
                                             // relative path (use configured downloadBaseUrl)
                                             withContext(Dispatchers.IO) {
                                                 // Legacy fallback for manual fetch: just get the bin
                                                 ModelUpdateUtil.downloadAndApplyModelWithLock(this@MainActivity, downloadBaseUrl, wsClient, AppConfig.getServerAuthToken(this@MainActivity), modelLink, null)
                                             }
                                         }

                                        if (ok) {
                                            Toast.makeText(this@MainActivity, "モデル取得と適用に成功しました。", Toast.LENGTH_LONG).show()
                                            RealTimeLogger.i("MainActivity", "Manual model fetch applied: $modelLink")
                                        } else {
                                            Toast.makeText(this@MainActivity, "モデル取得失敗: $msg", Toast.LENGTH_LONG).show()
                                            RealTimeLogger.w("MainActivity", "Manual model fetch failed: $msg")
                                        }
                                    } finally {
                                        com.example.hfl_experiment.network.FetchInFlightRegistry.inFlight.set(false)
                                    }
                                }
                            },
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 8.dp)
                        ) {
                            Text("Fetch New Model (手動取得)")
                        }

                        // DEBUG: Test upload to exercise uploadFlatWeights and produce logs
                        Button(
                            onClick = {
                                scope.launch {
                                    if (!com.example.hfl_experiment.network.FetchInFlightRegistry.inFlight.compareAndSet(false, true)) {
                                        Toast.makeText(this@MainActivity, "既に手動処理中です。", Toast.LENGTH_SHORT).show()
                                        return@launch
                                    }
                                    try {
                                        Toast.makeText(this@MainActivity, "アップロード試験を開始します...（ログを確認）", Toast.LENGTH_SHORT).show()
                                        try {
                                            // 1) fetch meta
                                            val meta = withContext(Dispatchers.IO) { networkClientInstance.fetchMeta(forceRefresh = true) }

                                            // 2) build tiny dummy flat weights
                                            val tpRttFeatures = 2
                                            val appNumMax = appConfigs?.size ?: AppConfig.APP_CAT_COUNT
                                            val inputSize = tpRttFeatures + appNumMax
                                            val hiddenSize = 8
                                            val outputSize = appNumMax
                                            val dummy = FloatArray(inputSize * hiddenSize) { 0.01f }

                                            RealTimeLogger.i("MainActivity", "DEBUG upload: meta.round=${meta.round} model_id=${meta.model_id} base_hash=${meta.base_hash}")

                                            withContext(Dispatchers.IO) {
                                                try {
                                                    networkClientInstance.uploadFlatWeights(
                                                        terminalId = AppConfig.getTerminalId(applicationContext),
                                                        meta = meta,
                                                        nSamples = 1,
                                                        flatF32 = dummy,
                                                        fileName = "debug_weights.bin",
                                                        payloadKind = "debug",
                                                        hiddenSize = hiddenSize,
                                                        outputSize = outputSize,
                                                        inputSize = inputSize,
                                                        clientMetaJson = "{\"debug\":true}",
                                                        virtualRouterId = trainingViewModel.currentRouterId
                                                    )
                                                    RealTimeLogger.i("MainActivity", "DEBUG upload: uploadFlatWeights returned (check NetworkClient logs for response)")
                                                    withContext(Dispatchers.Main) {
                                                        Toast.makeText(this@MainActivity, "アップロード試験完了（詳細は logcat を確認）", Toast.LENGTH_LONG).show()
                                                    }
                                                } catch (e: Exception) {
                                                    RealTimeLogger.e("MainActivity", "DEBUG upload failed: ${e.message}", e)
                                                    withContext(Dispatchers.Main) {
                                                        Toast.makeText(this@MainActivity, "アップロード試験でエラー: ${e.message}", Toast.LENGTH_LONG).show()
                                                    }
                                                }
                                            }
                                        } catch (e: Exception) {
                                            RealTimeLogger.e("MainActivity", "DEBUG upload orchestration failed: ${e.message}", e)
                                            Toast.makeText(this@MainActivity, "試験前処理で失敗: ${e.message}", Toast.LENGTH_LONG).show()
                                        }
                                    } finally {
                                        com.example.hfl_experiment.network.FetchInFlightRegistry.inFlight.set(false)
                                    }
                                }
                            },
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 8.dp)
                        ) {
                            Text("DEBUG: Test Upload to Edge")
                        }
                        // DEBUG: Network diagnostic (TCP/DNS/HTTP) — shows result in Toast and log
                        Button(
                            onClick = {
                                scope.launch {
                                    try {
                                        Toast.makeText(this@MainActivity, "ネットワーク診断を実行します...", Toast.LENGTH_SHORT).show()
                                        val report = withContext(Dispatchers.IO) { networkClientInstance.diagnoseAll(3000) }
                                        RealTimeLogger.i("MainActivity", "Network diagnoseAll result:\n$report")
                                        Toast.makeText(this@MainActivity, "診断完了（logcatを確認）", Toast.LENGTH_LONG).show()
                                    } catch (e: Exception) {
                                        RealTimeLogger.e("MainActivity", "diagnoseAll failed: ${e.message}", e)
                                        Toast.makeText(this@MainActivity, "診断失敗: ${e.message}", Toast.LENGTH_LONG).show()
                                    }
                                }
                            },
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 8.dp)
                        ) {
                            Text("DEBUG: Network Diagnose")
                        }

                        // ネットワーク切替を提案ボタン（簡潔・安全実装）
                        Button(
                            modifier = Modifier.fillMaxWidth().padding(top = 16.dp),
                            onClick = {
                                scope.launch {
                                    try {
                                        // compute simple satisfaction score and show suggestion toast
                                        val appCfg = appConfigs?.firstOrNull()
                                        val appNeedRtt = appCfg?.needRTT?.toDouble() ?: 100.0
                                        val appNeedTp = appCfg?.needTP?.toDouble() ?: 5.0
                                        val probeUrl = AppConfig.getEdgeBaseUrl(this@MainActivity).trimEnd('/') + "/static/probe.bin"
                                         val linkTpMbps = try { NetworkBandwidthProbe.measureMbps(wsClient, probeUrl) ?: 5.0 } catch (_: Exception) { 5.0 }
                                         val linkRttMs = rttValue.value?.toDouble() ?: 120.0
                                         val group = if (appCfg?.indicator?.uppercase() == "RTT") SatisfactionUtil.AppGroup.G_RTT else SatisfactionUtil.AppGroup.G_TP
                                         val s = try { SatisfactionUtil.compute(group, linkRttMs, appNeedRtt) } catch (_: Exception) { 0.5 }
                                         val threshold = 0.8
                                         val shouldSwitch = s < threshold
                                         val msg = if (shouldSwitch) "提案: ネットワーク切替を検討してください (S=${"%.2f".format(s)})" else "維持を推奨します (S=${"%.2f".format(s)})"
                                         Toast.makeText(this@MainActivity, msg, Toast.LENGTH_SHORT).show()
                                     } catch (e: Exception) {
                                         RealTimeLogger.w("MainActivity", "net switch suggestion failed: ${e.message}")
                                         Toast.makeText(this@MainActivity, "診断に失敗しました", Toast.LENGTH_SHORT).show()
                                     }
                                 }
                             }
                         ) { Text("ネットワーク切替を提案 (満足度+ヒステリシス)") }

                        // 設定画面へ遷移
                        Button(
                            onClick = {
                                val intent = Intent(this@MainActivity, SettingsActivity::class.java)
                                startActivity(intent)
                            },
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 8.dp)
                        ) {
                            Text("環境・ネットワーク設定")
                        }
                    }
                }
            }
        }
    }

    // helper to safely load app.json and extract `appType` list
    private fun loadAppTypesSafe(context: Context): List<AppConfigModel>? {
        try {
            // attempt to read app.json from filesDir, external files, or assets
            val candidates = mutableListOf<File>()
            candidates.add(File(context.filesDir, "app.json"))
            context.getExternalFilesDir(null)?.let { candidates.add(File(it, "app.json")) }
            for (f in candidates) {
                 if (f.exists()) {
                     try {
                         val txt = f.readText()
                         val json = Json.parseToJsonElement(txt)
                         return json.jsonArray.mapNotNull { elem ->
                             try {
                                 val o = elem.jsonObject
                                 val appType = o["appType"]?.jsonPrimitive?.content ?: return@mapNotNull null
                                 val indicator = o["indicator"]?.jsonPrimitive?.content ?: ""
                                 val needRTT = o["needRTT"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
                                 val needTP = o["needTP"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
                                 com.example.hfl_experiment.model.AppConfig(appType = appType, indicator = indicator, needRTT = needRTT, needTP = needTP, incRTT = 0.0, incTP = 0.0)
                             } catch (_: Exception) {
                                 null
                             }
                         }
                     } catch (_: Exception) { }
                 }
             }

             // try assets
             try {
                 context.assets.open("app.json").use { ins ->
                     val txt = ins.bufferedReader().readText()
                     val json = Json.parseToJsonElement(txt)
                     return json.jsonArray.mapNotNull { elem ->
                         try {
                             val o = elem.jsonObject
                             val appType = o["appType"]?.jsonPrimitive?.content ?: return@mapNotNull null
                             val indicator = o["indicator"]?.jsonPrimitive?.content ?: ""
                             val needRTT = o["needRTT"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
                             val needTP = o["needTP"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
                             com.example.hfl_experiment.model.AppConfig(appType = appType, indicator = indicator, needRTT = needRTT, needTP = needTP, incRTT = 0.0, incTP = 0.0)
                         } catch (_: Exception) { null }
                     }
                 }
             } catch (_: Exception) { }

         } catch (e: Exception) {
            RealTimeLogger.w("MainActivity", "loadAppTypesSafe failed: ${e.message}")
        }
        return null
    }

    // Sample DTO for validation usages
    data class SampleDto(val id: Int = 0, val name: String = "Sample")

    private fun getMyTerminalIndex(): Int? {
        return try {
            val tid = AppConfig.getTerminalId(this)
            // expect formats like device-001 or device001; extract trailing number
            val m = Regex("(\\d+)").find(tid ?: "")
            val num = m?.value?.toIntOrNull()
            if (num != null) {
                // convert 1-based device number to 0-based index
                return num - 1
            }
            null
        } catch (e: Exception) {
            RealTimeLogger.w("MainActivity", "getMyTerminalIndex failed: ${e.message}")
            null
        }
    }

    private fun getCurrentApIndex(): Int {
        return try {
            val currentSsid = com.example.hfl_experiment.util.networking.NetworkSwitchHelper.getConnectedSsid(this)
            val a = AppConfig.getPrimarySsid(this)
            val b = AppConfig.getSecondarySsid(this)
            when {
                currentSsid == null -> -1
                currentSsid == a -> 0
                currentSsid == b -> 1
                else -> -1
            }
        } catch (e: Exception) {
            RealTimeLogger.w("MainActivity", "getCurrentApIndex failed: ${e.message}")
            -1
        }
    }

    private fun setCurrentApIndex(idx: Int) {
        try {
            val prefs = this.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
            prefs.edit().putInt("current_ap_index", idx).apply()
        } catch (e: Exception) {
            RealTimeLogger.w("MainActivity", "setCurrentApIndex failed: ${e.message}")
        }
    }

    override fun onDestroy() {
        try {
            currentWsListener?.stop()
            currentWsListener = null
            wsClient.dispatcher.executorService.shutdown()
            wsClient.connectionPool.evictAll()
        } catch (_: Exception) {}
        super.onDestroy()
    }

    // Attempt to switch to target AP index (0 -> primary, 1 -> secondary). Returns true if connected.
    private suspend fun attemptSwitchToAp(targetIndex: Int, maxAttempts: Int = 3, initialBackoffMs: Long = 1000L): Boolean {
        return withContext(Dispatchers.IO) {
            try {
                val ssid = when (targetIndex) {
                    0 -> AppConfig.getPrimarySsid(this@MainActivity)
                    1 -> AppConfig.getSecondarySsid(this@MainActivity)
                    else -> null
                }
                val pass = when (targetIndex) {
                    0 -> AppConfig.ROUTER_PASS_A
                    1 -> AppConfig.ROUTER_PASS_B
                    else -> null
                }
                if (ssid.isNullOrBlank()) return@withContext false

                var attempt = 0
                var backoff = initialBackoffMs
                while (attempt < maxAttempts) {
                    attempt++
                    RealTimeLogger.i("MainActivity", "attemptSwitchToAp: attempt=${attempt} ssid=${ssid}")
                    val status = try { com.example.hfl_experiment.util.networking.NetworkSwitchHelper.suggestWifi(applicationContext, ssid, pass) } catch (e: Exception) { -99 }
                    // If suggestion returns 0 or positive, we consider it requested; otherwise we may have opened settings (negative codes)
                    RealTimeLogger.i("MainActivity", "suggestWifi returned status=${status} for ssid=${ssid}")

                    // Wait briefly and then poll connection
                    try {
                        // awaitWifiConnected blocks; run it but limit to 20s per attempt
                        val connected = com.example.hfl_experiment.util.networking.NetworkSwitchHelper.awaitWifiConnected(applicationContext, timeoutMs = 20_000L) { newSsid ->
                            RealTimeLogger.i("MainActivity", "awaitWifiConnected update: ssid=${newSsid}")
                        }
                        if (connected) {
                            // verify connected to desired SSID
                            val nowSsid = com.example.hfl_experiment.util.networking.NetworkSwitchHelper.getConnectedSsid(applicationContext)
                            if (!nowSsid.isNullOrBlank() && nowSsid == ssid) {
                                RealTimeLogger.i("MainActivity", "Connected to target ssid=${ssid}")
                                setCurrentApIndex(targetIndex)
                                return@withContext true
                            }
                        }
                    } catch (e: Exception) {
                        RealTimeLogger.w("MainActivity", "awaitWifiConnected attempt failed: ${e.message}")
                    }

                    // backoff
                    try { Thread.sleep(backoff) } catch (_: InterruptedException) {}
                    backoff = (backoff * 2).coerceAtMost(30_000L)
                }

                RealTimeLogger.w("MainActivity", "attemptSwitchToAp: all attempts failed for ssid=${ssid}")
                return@withContext false
            } catch (e: Exception) {
                RealTimeLogger.w("MainActivity", "attemptSwitchToAp exception: ${e.message}")
                return@withContext false
            }
        }
    }
}
