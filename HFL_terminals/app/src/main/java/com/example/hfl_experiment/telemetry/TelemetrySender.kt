package com.example.hfl_experiment.telemetry

import android.content.Context
import android.os.BatteryManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import android.os.SystemClock
import android.provider.Settings
import java.util.Locale
import org.json.JSONObject
import com.example.hfl_experiment.network.NetworkClient
import com.example.hfl_experiment.AppConfig
import java.io.File
import com.example.hfl_experiment.util.logging.RealTimeLogger
import com.example.hfl_experiment.training.TrainingMetricsStore
import android.content.Intent
import android.content.IntentFilter
import com.example.hfl_experiment.experiment.ExperimentContext

object TelemetrySender {
    private const val TAG = "TelemetrySender"

    fun gatherTelemetry(context: Context): JSONObject {
        RealTimeLogger.i(TAG, "gatherTelemetry: start")
        val jo = JSONObject()
        try {
            // Add device id explicitly (prefer configured terminal id, fallback to package name)
            try { jo.putOpt("device_id", AppConfig.getTerminalId(context)) } catch (_: Exception) { jo.putOpt("device_id", context.packageName ?: "") }

            // Terminal id (app-level identifier)
            try { jo.putOpt("terminal_id", AppConfig.getTerminalId(context)) } catch (_: Exception) { jo.putOpt("terminal_id", "") }

            // 実験セッション追跡フィールド（サーバーログとの突き合わせに必須）
            try { jo.put("run_id", ExperimentContext.runId.ifEmpty { "none" }) } catch (_: Exception) {}
            try { jo.put("experiment_group", ExperimentContext.experimentGroup) } catch (_: Exception) {}
            try { jo.put("current_round", ExperimentContext.currentRound) } catch (_: Exception) {}
            try { jo.put("model_version", ExperimentContext.modelVersion) } catch (_: Exception) {}
            try { jo.put("run_started_at_ms", ExperimentContext.runStartedAtMs) } catch (_: Exception) {}

            // android id (masked) - may be null on some devices; mask for privacy
            try {
                val aid = try { Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID) } catch (_: Exception) { null }
                if (!aid.isNullOrBlank()) {
                    val md = java.security.MessageDigest.getInstance("SHA-256")
                    val h = md.digest(aid.toByteArray(Charsets.UTF_8))
                    val masked = h.joinToString("") { "%02x".format(it) }.take(12)
                    jo.putOpt("android_id_masked", masked)
                } else jo.put("android_id_masked", "")
            } catch (e: Exception) { RealTimeLogger.w(TAG, "gatherTelemetry: android_id read failed: ${e.message}") }

            // battery - try multiple ways and use sentinel if unknown
            try {
                var battPct: Double? = null
                try {
                    val bm = context.getSystemService(BatteryManager::class.java)
                    val batt = bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
                    if (batt != null && batt >= 0) battPct = batt / 100.0
                } catch (_: Exception) {}

                // fallback to ACTION_BATTERY_CHANGED
                if (battPct == null) {
                    try {
                        val intent = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
                        if (intent != null) {
                            val level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
                            val scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
                            if (level >= 0 && scale > 0) battPct = level.toDouble() / scale.toDouble()
                        }
                    } catch (_: Exception) {}
                }

                if (battPct != null) jo.put("battery_level", battPct) else jo.put("battery_level", -1.0)
            } catch (e: Exception) { RealTimeLogger.w(TAG, "gatherTelemetry: battery read failed: ${e.message}"); jo.put("battery_level", -1.0) }

            // 充電状態と温度（学習中の熱throttlingや電力消費の分析に有用）
            try {
                val intent = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
                if (intent != null) {
                    val status = intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
                    val isCharging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
                                     status == BatteryManager.BATTERY_STATUS_FULL
                    jo.put("is_charging", isCharging)
                    val plugged = intent.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1)
                    jo.put("charge_source", when (plugged) {
                        BatteryManager.BATTERY_PLUGGED_AC -> "ac"
                        BatteryManager.BATTERY_PLUGGED_USB -> "usb"
                        BatteryManager.BATTERY_PLUGGED_WIRELESS -> "wireless"
                        0 -> "none"
                        else -> "unknown"
                    })
                    // 温度は 10 分の 1 摂氏で返ってくる
                    val tempRaw = intent.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1)
                    jo.put("battery_temperature_c", if (tempRaw >= 0) tempRaw / 10.0 else -1.0)
                } else {
                    jo.put("is_charging", false)
                    jo.put("battery_temperature_c", -1.0)
                    jo.put("charge_source", "unknown")
                }
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "gatherTelemetry: charging state read failed: ${e.message}")
                jo.put("is_charging", false)
                jo.put("battery_temperature_c", -1.0)
                jo.put("charge_source", "unknown")
            }

            // timestamp and basic device fields
            try { jo.putOpt("timestamp_ms", System.currentTimeMillis()) } catch (_: Exception) {}
            try { jo.putOpt("device_model", Build.MODEL ?: "") } catch (_: Exception) {}
            try { jo.putOpt("device_manufacturer", Build.MANUFACTURER ?: "") } catch (_: Exception) {}
            try { jo.putOpt("os_sdk_int", Build.VERSION.SDK_INT) } catch (_: Exception) {}

            // uptime
            try { jo.putOpt("uptime_ms", SystemClock.elapsedRealtime()) } catch (_: Exception) {}

            // locale
            try { jo.putOpt("locale", Locale.getDefault().toLanguageTag()) } catch (_: Exception) {}

            // network
            try {
                // include current AP index & masked ssid for router selection debug
                try {
                    val prefs = context.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
                    val currentAp = prefs.getInt("current_ap_index", -1)
                    jo.putOpt("current_ap_index", currentAp)
                } catch (_: Exception) {}

                val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
                val net = cm?.activeNetwork
                val caps = net?.let { cm.getNetworkCapabilities(it) }
                val nt = when {
                    caps == null -> "UNKNOWN"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "WIFI"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "CELLULAR"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ETHERNET"
                    else -> "OTHER"
                }
                jo.putOpt("network_type", nt)

                // Avoid telephony calls that require runtime permissions; provide safe defaults
                jo.put("network_subtype", -1)
                jo.putOpt("network_operator_name", "")

                // try to include rssi/link speed if wifi - mask SSID/BSSID for privacy; include bandwidth hints
                try {
                    // link bandwidth from NetworkCapabilities
                    val downKbps = caps?.linkDownstreamBandwidthKbps ?: -1
                    val upKbps = caps?.linkUpstreamBandwidthKbps ?: -1
                    jo.put("network_down_kbps", downKbps)
                    jo.put("network_up_kbps", upKbps)

                    val wm = context.getSystemService(Context.WIFI_SERVICE) as? android.net.wifi.WifiManager
                    val info = wm?.connectionInfo
                    if (info != null) {
                        val ssid = info.ssid ?: ""
                        val bssid = info.bssid ?: ""
                        // store masked ssid name separately for clearer telemetry
                        val maskedName = try {
                            val md = java.security.MessageDigest.getInstance("SHA-256")
                            val h = md.digest(ssid.toByteArray(Charsets.UTF_8))
                            h.joinToString("") { "%02x".format(it) }.take(12)
                        } catch (_: Exception) { "" }
                        jo.putOpt("wifi_ssid_name_masked", if (ssid.isNotEmpty()) maskedName else "")
                        fun mask(s: String): String {
                            return try {
                                val md = java.security.MessageDigest.getInstance("SHA-256")
                                val h = md.digest(s.toByteArray(Charsets.UTF_8))
                                h.joinToString("") { "%02x".format(it) }.take(8)
                            } catch (_: Exception) { "" }
                        }
                        jo.putOpt("wifi_ssid_masked", if (ssid.isNotEmpty()) mask(ssid) else "")
                        jo.putOpt("wifi_bssid_masked", if (bssid.isNotEmpty()) mask(bssid) else "")
                        jo.put("wifi_rssi_dbm", if (info.rssi != Int.MIN_VALUE) info.rssi else -999)
                        jo.put("wifi_link_speed_mbps", if (info.linkSpeed >= 0) info.linkSpeed else -1)
                    } else {
                        jo.putOpt("wifi_ssid_masked", "")
                        jo.putOpt("wifi_bssid_masked", "")
                        jo.put("wifi_rssi_dbm", -999)
                        jo.put("wifi_link_speed_mbps", -1)
                    }
                } catch (e: Exception) { RealTimeLogger.w(TAG, "gatherTelemetry: wifi info read failed: ${e.message}"); jo.putOpt("wifi_ssid_masked", ""); jo.putOpt("wifi_bssid_masked", ""); jo.put("wifi_rssi_dbm", -999); jo.put("wifi_link_speed_mbps", -1) }

            } catch (e: Exception) { RealTimeLogger.w(TAG, "gatherTelemetry: network info read failed: ${e.message}") }

            // If RTT probe results persisted by other components, include a small preview
            try {
                val rttFile = File(context.filesDir, "rtt_last_ms.txt")
                if (rttFile.exists()) {
                    val txt = rttFile.readText().trim()
                    val v = txt.toDoubleOrNull()
                    if (v != null) jo.put("rtt_last_ms", v)
                }
            } catch (_: Exception) {}

            // storage
            try {
                val stat = android.os.StatFs(context.filesDir.absolutePath)
                val free = try { stat.availableBytes / 1024 / 1024 } catch (_: Exception) { -1 }
                val total = try { (stat.blockCountLong * stat.blockSizeLong) / 1024 / 1024 } catch (_: Exception) { -1 }
                jo.put("storage_free_mb", if (free >= 0) free else -1)
                jo.put("storage_total_mb", if (total >= 0) total else -1)
            } catch (e: Exception) { RealTimeLogger.w(TAG, "gatherTelemetry: storage read failed: ${e.message}"); jo.put("storage_free_mb", -1); jo.put("storage_total_mb", -1) }

            // simple cpu/mem
            try {
                val rt = Runtime.getRuntime()
                val dalvikUsedKb = try { (rt.totalMemory() - rt.freeMemory()) / 1024 } catch (_: Exception) { -1L }
                val dalvikMaxKb = try { rt.maxMemory() / 1024 } catch (_: Exception) { -1L }
                // native heap (approx) via Debug API
                val nativeAllocKb = try { android.os.Debug.getNativeHeapAllocatedSize() / 1024 } catch (_: Exception) { -1L }
                val nativeSizeKb = try { android.os.Debug.getNativeHeapSize() / 1024 } catch (_: Exception) { -1L }
                // put both KB and MB convenience fields
                jo.put("mem_dalvik_used_kb", dalvikUsedKb)
                jo.put("mem_dalvik_max_kb", dalvikMaxKb)
                jo.put("mem_dalvik_used_mb", if (dalvikUsedKb >= 0) dalvikUsedKb / 1024.0 else -1.0)
                jo.put("mem_dalvik_max_mb", if (dalvikMaxKb >= 0) dalvikMaxKb / 1024.0 else -1.0)
                jo.put("mem_native_alloc_kb", nativeAllocKb)
                jo.put("mem_native_size_kb", nativeSizeKb)
                jo.put("mem_native_alloc_mb", if (nativeAllocKb >= 0) nativeAllocKb / 1024.0 else -1.0)
                // mem_used_mb: dalvik + native total (server Pydantic field name)
                val totalUsedMb = when {
                    dalvikUsedKb >= 0 && nativeAllocKb >= 0 -> (dalvikUsedKb + nativeAllocKb) / 1024.0
                    dalvikUsedKb >= 0 -> dalvikUsedKb / 1024.0
                    else -> -1.0
                }
                jo.put("mem_used_mb", totalUsedMb)
                jo.put("cpu_cores", Runtime.getRuntime().availableProcessors())
            } catch (e: Exception) { RealTimeLogger.w(TAG, "gatherTelemetry: mem/cpu read failed: ${e.message}"); jo.put("mem_used_mb", -1); jo.put("mem_max_mb", -1); jo.put("cpu_cores", 1) }

            // app version
            try {
                val pm = context.packageManager
                val pi = pm.getPackageInfo(context.packageName, 0)
                jo.putOpt("app_version", pi.versionName ?: "")
                jo.putOpt("app_version_code", if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) pi.longVersionCode else pi.versionCode)
            } catch (e: Exception) { RealTimeLogger.w(TAG, "gatherTelemetry: app version read failed: ${e.message}"); jo.putOpt("app_version", ""); jo.putOpt("app_version_code", -1) }

            // Training metrics summary inclusion
            try {
                try { TrainingMetricsStore.init(context) } catch (_: Exception) {}
                val all = try { TrainingMetricsStore.listAll() } catch (_: Exception) { null }
                if (!all.isNullOrBlank()) {
                    val lines = all.trim().lines().filter { it.isNotBlank() }
                    jo.put("training_metrics_count", lines.size)
                    val last = lines.lastOrNull()
                    if (!last.isNullOrBlank()) {
                        try {
                            val lastJo = JSONObject(last)
                            jo.put("training_metrics_last", lastJo)
                            if (lastJo.has("accuracy")) jo.put("training_metrics_last_accuracy", lastJo.optDouble("accuracy")) else jo.put("training_metrics_last_accuracy", -1.0)
                            if (lastJo.has("loss")) jo.put("training_metrics_last_loss", lastJo.optDouble("loss")) else jo.put("training_metrics_last_loss", -1.0)
                            if (lastJo.has("round")) jo.put("training_metrics_last_round", lastJo.optInt("round")) else jo.put("training_metrics_last_round", -1)
                            // app_type をトップレベルに昇格（推論スナップショットで参照される）
                            if (lastJo.has("app_type") && !jo.has("app_type")) {
                                jo.put("app_type", lastJo.optString("app_type", "unknown"))
                            }
                        } catch (e: Exception) { RealTimeLogger.w(TAG, "gatherTelemetry: parse last training metric failed: ${e.message}") }
                    }
                } else {
                    jo.put("training_metrics_count", 0)
                    jo.put("training_metrics_last_accuracy", -1.0)
                    jo.put("training_metrics_last_loss", -1.0)
                    jo.put("training_metrics_last_round", -1)
                }
            } catch (e: Exception) { RealTimeLogger.w(TAG, "gatherTelemetry: training metrics read failed: ${e.message}"); jo.put("training_metrics_count", 0) }

            // Device metrics for accuracy degradation analysis
            try {
                val dm = com.example.hfl_experiment.telemetry.DeviceMetricsCollector.snapshot()
                jo.put("device_metrics", dm)
            } catch (_: Exception) {}

            // app_type がまだ設定されていない場合、SharedPreferences + assets/app.json から取得
            try {
                if (!jo.has("app_type") || jo.optString("app_type").isNullOrBlank() || jo.optString("app_type") == "unknown") {
                    val prefs = context.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
                    val appIdx = prefs.getInt("assigned_app_index", -1)
                    if (appIdx >= 0) {
                        val appJsonStr = context.assets.open("app.json").bufferedReader().use { it.readText() }
                        val arr = org.json.JSONArray(appJsonStr)
                        if (appIdx < arr.length()) {
                            val appType = arr.getJSONObject(appIdx).optString("appType", "unknown")
                            jo.put("app_type", appType)
                        }
                    }
                }
            } catch (e: Exception) { RealTimeLogger.w(TAG, "gatherTelemetry: app_type resolve failed: ${e.message}") }

            // Terminal satisfaction (if measured and persisted by TrainingViewModel)
            try {
                val satFile = File(context.filesDir, "terminal_satisfaction.json")
                if (satFile.exists()) {
                    val txt = satFile.readText()
                    val sjo = JSONObject(txt)
                    // expose raw meta for debugging/analysis
                    jo.putOpt("terminal_satisfaction_meta", sjo)

                    // Prefer explicit before/after fields if provided by the measurement flow
                    val hasBefore = sjo.has("satisfaction_before")
                    val hasAfter = sjo.has("satisfaction_after")
                    val beforeVal = if (hasBefore) sjo.optDouble("satisfaction_before") else Double.NaN
                    val afterVal = if (hasAfter) sjo.optDouble("satisfaction_after") else Double.NaN
                    val avgVal = if (sjo.has("satisfaction_avg")) sjo.optDouble("satisfaction_avg") else Double.NaN

                    if (hasBefore) {
                        try { jo.put("terminal_satisfaction_before", beforeVal) } catch (_: Exception) { jo.putOpt("terminal_satisfaction_before", org.json.JSONObject.NULL) }
                    } else {
                        jo.putOpt("terminal_satisfaction_before", org.json.JSONObject.NULL)
                    }

                    if (hasAfter) {
                        try { jo.put("terminal_satisfaction_after", afterVal) } catch (_: Exception) { jo.putOpt("terminal_satisfaction_after", org.json.JSONObject.NULL) }
                    } else {
                        jo.putOpt("terminal_satisfaction_after", org.json.JSONObject.NULL)
                    }

                    // Choose representative top-level satisfaction: prefer after -> before -> avg -> legacy 'satisfaction' -> NULL
                    when {
                        hasAfter -> jo.put("terminal_satisfaction", afterVal)
                        hasBefore -> jo.put("terminal_satisfaction", beforeVal)
                        !avgVal.isNaN() -> jo.put("terminal_satisfaction", avgVal)
                        sjo.has("satisfaction") -> jo.put("terminal_satisfaction", sjo.optDouble("satisfaction"))
                        else -> jo.putOpt("terminal_satisfaction", org.json.JSONObject.NULL)
                    }

                    // Compute delta if both before and after are available
                     try {
                         if (hasBefore && hasAfter && !beforeVal.isNaN() && !afterVal.isNaN()) {
                             val delta = afterVal - beforeVal
                             jo.put("terminal_satisfaction_delta", delta)
                         } else {
                             jo.putOpt("terminal_satisfaction_delta", org.json.JSONObject.NULL)
                         }
                     } catch (e: Exception) {
                         RealTimeLogger.w(TAG, "gatherTelemetry: failed to compute satisfaction delta: ${e.message}")
                         jo.putOpt("terminal_satisfaction_delta", org.json.JSONObject.NULL)
                     }
                } else {
                    jo.putOpt("terminal_satisfaction", org.json.JSONObject.NULL)
                }
            } catch (e: Exception) { RealTimeLogger.w(TAG, "gatherTelemetry: failed to read terminal_satisfaction.json: ${e.message}"); jo.putOpt("terminal_satisfaction", org.json.JSONObject.NULL) }

            // session logs count
            try {
                val logs = RealTimeLogger.getLogFiles(context)
                jo.put("session_log_count", logs.size)
                jo.put("session_log_paths_preview", logs.take(5).map { File(it.absolutePath).name })
            } catch (e: Exception) { RealTimeLogger.w(TAG, "gatherTelemetry: log files read failed: ${e.message}"); jo.put("session_log_count", 0); jo.put("session_log_paths_preview", org.json.JSONArray()) }

        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "gatherTelemetry: unexpected error: ${e.message}")
        }
        RealTimeLogger.i(TAG, "gatherTelemetry: done keys=${jo.keys().asSequence().toList().joinToString(",")}")
        return jo
    }

    // Complement missing fields with safe defaults; return list of complemented keys
    private fun complementTelemetry(jo: JSONObject, context: Context): List<String> {
        val complemented = mutableListOf<String>()
        try {
            if (!jo.has("timestamp_ms") || jo.isNull("timestamp_ms")) {
                jo.put("timestamp_ms", System.currentTimeMillis())
                complemented.add("timestamp_ms")
            }
        } catch (e: Exception) { RealTimeLogger.w(TAG, "complementTelemetry: timestamp failed: ${e.message}") }
        try {
            if (!jo.has("device_model") || jo.isNull("device_model") || jo.optString("device_model").isBlank()) {
                jo.put("device_model", Build.MODEL ?: "unknown")
                complemented.add("device_model")
            }
        } catch (e: Exception) { RealTimeLogger.w(TAG, "complementTelemetry: device_model failed: ${e.message}") }
        try {
            if (!jo.has("network_type") || jo.isNull("network_type") || jo.optString("network_type").isBlank()) {
                val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
                val net = cm?.activeNetwork
                val caps = net?.let { cm.getNetworkCapabilities(it) }
                val nt = when {
                    caps == null -> "UNKNOWN"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "WIFI"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "CELLULAR"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ETHERNET"
                    else -> "OTHER"
                }
                jo.put("network_type", nt)
                complemented.add("network_type")
            }
        } catch (e: Exception) { RealTimeLogger.w(TAG, "complementTelemetry: network_type failed: ${e.message}") }
        try {
            if (!jo.has("os_sdk_int") || jo.isNull("os_sdk_int")) {
                try { jo.put("os_sdk_int", Build.VERSION.SDK_INT); complemented.add("os_sdk_int") } catch (e: Exception) { RealTimeLogger.w(TAG, "complementTelemetry: os_sdk_int failed: ${e.message}") }
            }
        } catch (e: Exception) { RealTimeLogger.w(TAG, "complementTelemetry: unexpected error: ${e.message}") }

        // Ensure battery_level exists (use -1.0 if unknown)
        try {
            if (!jo.has("battery_level") || jo.isNull("battery_level")) {
                jo.put("battery_level", -1.0)
                complemented.add("battery_level")
            }
        } catch (e: Exception) { RealTimeLogger.w(TAG, "complementTelemetry: battery_level failed: ${e.message}") }

        // mark complemented fields array
        try {
            val arr = org.json.JSONArray()
            complemented.forEach { arr.put(it) }
            if (arr.length() > 0) jo.put("client_complemented", arr)
        } catch (e: Exception) { RealTimeLogger.w(TAG, "complementTelemetry: cannot write complemented array: ${e.message}") }

        RealTimeLogger.i(TAG, "complementTelemetry: complemented=${complemented.joinToString(",")}")
        return complemented
    }

    // Validate telemetry before send (basic checks + client_meta size if present)
    fun validateTelemetryBeforeSend(jo: JSONObject): Boolean {
        try {
            // ensure required fields exist (they may be filled by complementTelemetry before calling)
            if (!jo.has("timestamp_ms")) { RealTimeLogger.w(TAG, "validateTelemetryBeforeSend: missing timestamp_ms"); return false }
            if (!jo.has("device_model")) { RealTimeLogger.w(TAG, "validateTelemetryBeforeSend: missing device_model"); return false }
            if (!jo.has("network_type")) { RealTimeLogger.w(TAG, "validateTelemetryBeforeSend: missing network_type"); return false }

            // client_meta length check if present
            val clientMeta = jo.optString("client_meta", "")
            if (clientMeta.isNotEmpty()) {
                val bytes = clientMeta.toByteArray(Charsets.UTF_8)
                val max = 8 * 1024
                if (bytes.size > max) { RealTimeLogger.w(TAG, "validateTelemetryBeforeSend: client_meta too large (${bytes.size} > $max)"); return false }
            }

            return true
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "validateTelemetryBeforeSend: exception ${e.message}")
            return false
        }
    }

    suspend fun send(context: Context) : Boolean {
        RealTimeLogger.i(TAG, "send: start")
        val client = NetworkClient(context, AppConfig.getEdgeBaseUrl(context), authToken = AppConfig.getServerAuthToken(context))
        val jo = gatherTelemetry(context)

        // complement missing fields, record what was added
        complementTelemetry(jo, context)

        // dump telemetry to filesDir for debugging / server-side reconciliation
        try {
            val f = File(context.filesDir, "telemetry_last.json")
            f.writeText(jo.toString())
            RealTimeLogger.i(TAG, "send: telemetry dumped to ${f.absolutePath} size=${f.length()}")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "send: failed to dump telemetry locally: ${e.message}")
        }

        // validate before send
        if (!validateTelemetryBeforeSend(jo)) {
            // write a short failure note and skip send; operator can inspect telemetry_last.json
            try {
                val f = File(context.filesDir, "telemetry_last_invalid.json")
                f.writeText(jo.toString())
                RealTimeLogger.w(TAG, "send: validation failed, telemetry written to telemetry_last_invalid.json")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "send: failed to write invalid telemetry file: ${e.message}")
            }
            return false
        }

        val terminalId = AppConfig.getTerminalId(context)
        RealTimeLogger.i(TAG, "send: invoking NetworkClient.sendTelemetry terminalId=$terminalId")
        try {
            val ok = client.sendTelemetry(terminalId, jo.toString())
            RealTimeLogger.i(TAG, "send: NetworkClient.sendTelemetry returned=$ok")

            // record server response / last send metadata for reconciliation
            try {
                val out = File(context.filesDir, "telemetry_last_sent.json")
                val record = JSONObject()
                record.put("sent_at_ms", System.currentTimeMillis())
                record.put("success", ok)
                record.put("payload_size", jo.toString().toByteArray(Charsets.UTF_8).size)
                out.writeText(record.toString())
                RealTimeLogger.i(TAG, "send: telemetry_last_sent.json written size=${out.length()}")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "send: failed to write telemetry_last_sent.json: ${e.message}")
            }

            return ok
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "send: NetworkClient.sendTelemetry exception: ${e.message}")

            try {
                val out = File(context.filesDir, "telemetry_last_sent.json")
                val record = JSONObject()
                record.put("sent_at_ms", System.currentTimeMillis())
                record.put("success", false)
                record.put("error", e.message ?: "unknown")
                out.writeText(record.toString())
            } catch (_: Exception) { /* best-effort */ }

            return false
        }
    }
}
