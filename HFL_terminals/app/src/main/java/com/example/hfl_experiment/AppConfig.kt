package com.example.hfl_experiment

import android.content.Context
import android.net.wifi.WifiManager
import com.example.hfl_experiment.experiment.ExperimentContext
import com.example.hfl_experiment.util.logging.RealTimeLogger
import java.net.URI

/**
 * アプリ内の簡易設定。テスト用に定義しています。
 * 将来的に BuildConfig or secure storage に差し替えてください。
 */
object AppConfig {
    // SharedPreferences 名
    private const val PREFS_ENV = "env_prefs"
    private const val PREF_EDGE_ENDPOINT_CONFIGURED = "edge_endpoint_configured"
    /**
     * true のとき [getEdgeBaseUrl] は接続 Wi-Fi の SSID に応じてポートを 8001/8002 に書き換える。
     * 設定画面や初回セットアップでホスト+ポートを明示保存した場合は false にし、UI のポート（例: 8002）を尊重する。
     */
    private const val PREF_EDGE_SSID_PORT_REWRITE = "edge_ssid_port_rewrite"
    /** 同一 APK 運用: 初回に端末 ID をユーザーが確定したか（既存インストールは migrate で true 化） */
    private const val PREF_TERMINAL_IDENTITY_CONFIGURED = "terminal_identity_configured"

    // Fallback literals (mirror values currently set in app/build.gradle.kts)
    private const val DEFAULT_EDGE_BASE_URL = "http://192.168.11.6:8001"
    private const val DEFAULT_SERVER_AUTH_TOKEN = "" // Set via SettingsActivity

    // --- Memory optimization flag (runtime-switchable via SharedPreferences) ---
    fun isMemoryOptEnabled(context: Context): Boolean {
        val prefs = context.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
        return prefs.getBoolean("memory_opt_enabled", BuildConfig.MEMORY_OPT_DEFAULT)
    }
    fun setMemoryOptEnabled(context: Context, enabled: Boolean) {
        context.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
            .edit().putBoolean("memory_opt_enabled", enabled).apply()
    }

    // Helper: try to read BuildConfig.<field> via reflection to avoid compile-time dependency
    private fun tryGetBuildConfigFieldString(context: Context, fieldName: String): String? {
        return try {
            val pkg = context.packageName
            val cls = Class.forName("$pkg.BuildConfig")
            val f = cls.getDeclaredField(fieldName)
            f.isAccessible = true
            f.get(null) as? String
        } catch (_: Exception) {
            null
        }
    }

    /** 現在接続 Wi-Fi の SSID（引用符除去）。取得できなければ null。 */
    private fun readCurrentWifiSsid(context: Context): String? {
        return try {
            val wm = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val raw = wm.connectionInfo?.ssid
            if (raw.isNullOrBlank()) null else raw.trim('"')
        } catch (e: Exception) {
            RealTimeLogger.w("AppConfig", "readCurrentWifiSsid: ${e.message}")
            null
        }
    }

    /**
     * 研究室構成に合わせ、接続 AP（SSID）に応じてエッジ HTTP(S) ポートを 8001 / 8002 に切り替える。
     * [getPrimarySsid] → 8001、[getSecondarySsid] → 8002、それ以外・SSID 不明は 8001。
     * `ws://` / `wss://` およびパース不能な URL は変更しない。
     */
    private fun rewriteEdgePortForCurrentWifi(context: Context, baseUrl: String): String {
        val u = try {
            URI(baseUrl)
        } catch (_: Exception) {
            return baseUrl
        }
        val scheme = u.scheme?.lowercase() ?: return baseUrl
        if (scheme == "ws" || scheme == "wss") return baseUrl
        if (scheme != "http" && scheme != "https") return baseUrl
        val host = u.host ?: return baseUrl
        val ssid = readCurrentWifiSsid(context)
        val chosenPort = when (ssid) {
            getPrimarySsid(context) -> 8001
            getSecondarySsid(context) -> 8002
            else -> 8001
        }
        return "$scheme://$host:$chosenPort"
    }

    // BuildConfig の値をフォールバックとして使い、実行時に SettingsActivity 等で上書き可能にする
    // これにより管理ポイントを一箇所に集約できます。
    fun getEdgeBaseUrl(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        val edgeHost = prefs.getString("edge_host", null)?.trim()
        val edgePort = prefs.getString("edge_port", null)?.trim()

        val raw = if (!edgeHost.isNullOrBlank()) {
            if (edgeHost.startsWith("http://", ignoreCase = true)
                || edgeHost.startsWith("https://", ignoreCase = true)
                || edgeHost.startsWith("ws://", ignoreCase = true)
                || edgeHost.startsWith("wss://", ignoreCase = true)
            ) {
                edgeHost
            } else {
                val portPart = if (!edgePort.isNullOrBlank()) ":${edgePort}" else ""
                "http://$edgeHost$portPart"
            }
        } else {
            tryGetBuildConfigFieldString(context, "EDGE_BASE_URL") ?: DEFAULT_EDGE_BASE_URL
        }
        val ssidPortRewrite = prefs.getBoolean(PREF_EDGE_SSID_PORT_REWRITE, true)
        return if (ssidPortRewrite) rewriteEdgePortForCurrentWifi(context, raw) else raw
    }

    /**
     * 既存インストール: `edge_host` + `edge_port`、または `http(s)://…:port` 形式でポートが埋まっている場合は
     * SSID によるポート上書きを無効化する（アップデート前から 8002 を指定していた端末向け）。
     */
    fun migrateEdgeSsidPortRewriteFlagIfNeeded(context: Context) {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        if (prefs.contains(PREF_EDGE_SSID_PORT_REWRITE)) return
        val eh = prefs.getString("edge_host", null)?.trim().orEmpty()
        if (eh.isBlank()) return
        val ep = prefs.getString("edge_port", null)?.trim()
        var disableRewrite = false
        if (!ep.isNullOrBlank()) disableRewrite = true
        else if (eh.startsWith("http://", ignoreCase = true) || eh.startsWith("https://", ignoreCase = true)) {
            try {
                val u = URI(eh)
                if (u.port != -1) disableRewrite = true
            } catch (_: Exception) { }
        }
        if (disableRewrite) {
            prefs.edit().putBoolean(PREF_EDGE_SSID_PORT_REWRITE, false).apply()
            RealTimeLogger.i(
                "AppConfig",
                "migrateEdgeSsidPortRewrite: disabled SSID port rewrite (existing explicit endpoint)"
            )
        }
    }

    /** 設定保存時に false、既定ビルドに戻すときはキー削除で true 相当 */
    fun setEdgeSsidPortRewriteEnabled(context: Context, enabled: Boolean) {
        val e = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).edit()
        if (enabled) e.remove(PREF_EDGE_SSID_PORT_REWRITE) else e.putBoolean(PREF_EDGE_SSID_PORT_REWRITE, false)
        e.apply()
    }

    /** BuildConfig または [DEFAULT_EDGE_BASE_URL] のみ。`edge_host` の SharedPreferences は見ない。 */
    fun getCompiledDefaultEdgeBaseUrl(context: Context): String =
        tryGetBuildConfigFieldString(context, "EDGE_BASE_URL") ?: DEFAULT_EDGE_BASE_URL

    /**
     * ユーザーがエッジ接続先を確認済みか。初回は false のため起動時に接続先確認画面を出す。
     * 既存インストールでは `edge_host` が入っていれば確認済みとみなす（移行用）。
     */
    fun isEdgeEndpointUserConfirmed(context: Context): Boolean {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        if (prefs.getBoolean(PREF_EDGE_ENDPOINT_CONFIGURED, false)) return true
        val eh = prefs.getString("edge_host", null)?.trim()
        if (!eh.isNullOrBlank()) return true
        return false
    }

    fun markEdgeEndpointUserConfirmed(context: Context) {
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(PREF_EDGE_ENDPOINT_CONFIGURED, true)
            .apply()
    }

    /**
     * 既存インストール: `terminal_id` が env_prefs に既に入っている場合は確認済みとみなす（初回端末 ID 画面を出さない）。
     */
    fun migrateTerminalIdentityConfiguredFlagIfNeeded(context: Context) {
        val p = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        if (p.getBoolean(PREF_TERMINAL_IDENTITY_CONFIGURED, false)) return
        val tid = p.getString("terminal_id", null)?.trim()
        if (!tid.isNullOrBlank()) {
            p.edit().putBoolean(PREF_TERMINAL_IDENTITY_CONFIGURED, true).apply()
        }
    }

    fun isTerminalIdentityConfigured(context: Context): Boolean =
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
            .getBoolean(PREF_TERMINAL_IDENTITY_CONFIGURED, false)

    fun markTerminalIdentityConfigured(context: Context) {
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(PREF_TERMINAL_IDENTITY_CONFIGURED, true)
            .apply()
    }

    /** 端末 ID を変更した直後に ExperimentContext 等へ反映する */
    fun clearTerminalIdMemoryCache() {
        cachedTerminalId = null
    }

    /**
     * 初回セットアップで確定した端末 ID と実験群を保存する。
     */
    fun applyTerminalIdentity(context: Context, terminalId: String, experimentGroup: String) {
        val tid = terminalId.trim()
        val grp = experimentGroup.trim()
        val env = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        env.edit()
            .putString("terminal_id", tid)
            .putBoolean(PREF_TERMINAL_IDENTITY_CONFIGURED, true)
            .apply()
        cachedTerminalId = tid
        context.getSharedPreferences("experiment_context", Context.MODE_PRIVATE)
            .edit()
            .putString("experiment_group", grp)
            .apply()
        ExperimentContext.initFromAppConfig(context)
    }

    // サーバトークンも実行時に prefs で上書きできるようにする
    fun getServerAuthToken(context: Context): String? {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        val t = prefs.getString("server_auth_token", null)
        return if (!t.isNullOrBlank()) t else (tryGetBuildConfigFieldString(context, "SERVER_AUTH_TOKEN") ?: DEFAULT_SERVER_AUTH_TOKEN)
    }

    // Number of application categories (one-hot size / output classes). Change here to affect the whole app.
    const val APP_CAT_COUNT: Int = 4

    // NOTE: The following router credentials and secrets are currently hardcoded for testing.
    // It's strongly recommended to remove these and use secure storage / settings.
    const val ROUTER_SSID_A: String = "HFL_A24"
    const val ROUTER_PASS_A: String = "Tetsu060986239"
    const val ROUTER_BSSID_A: String = "" // 任意
    const val ROUTER_IP_A: String = "" // 任意

    const val ROUTER_SSID_B: String = "HFL_B"
    const val ROUTER_PASS_B: String = ""
    const val ROUTER_BSSID_B: String = "" // 任意
    const val ROUTER_IP_B: String = "" // 任意

    // SSID getters: allow runtime override via SharedPreferences, fallback to constants
    fun getPrimarySsid(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        val v = prefs.getString("router_ssid_a", null)?.trim()
        return if (!v.isNullOrBlank()) v else ROUTER_SSID_A
    }

    fun getSecondarySsid(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        val v = prefs.getString("router_ssid_b", null)?.trim()
        return if (!v.isNullOrBlank()) v else ROUTER_SSID_B
    }

    // WebSocket とダウンロードのパス（サーバ実装に合わせる）
    const val WS_PATH: String = "/ws/updates"
    const val DOWNLOAD_PATH: String = "/download"

    // --- New helpers to centralize URL construction ---
    // Returns download base like: http(s)://host:port/download
    fun getDownloadBaseUrl(context: Context): String {
        val base = getEdgeBaseUrl(context).trimEnd('/')
        return base + DOWNLOAD_PATH
    }

    // Returns websocket URL (ws:// or wss:// chosen based on edge base scheme) with optional token query
    fun getWebSocketUrl(context: Context, token: String?): String {
        val base = getEdgeBaseUrl(context)
        val wsScheme = if (base.startsWith("https")) "wss" else "ws"
        val uri = java.net.URI(base)
        val host = uri.host
        val port = if (uri.port != -1) ":${uri.port}" else ""
        val url = "$wsScheme://$host$port$WS_PATH"
        return if (token.isNullOrBlank()) url else "$url?token=$token"
    }

    // --- Device Config Loader ---
    // Load device_config.json from assets if present, and cache into SharedPreferences.
    // This allows per-device configuration to be injected via assets at build time.
    fun loadDeviceConfig(context: Context) {
        try {
            val am = context.assets
            // Check for device_config.json or any device_config.*.json in assets
            val list = am.list("") ?: emptyArray()
            var deviceFileName: String? = null
            if (list.contains("device_config.json")) {
                deviceFileName = "device_config.json"
            } else {
                // pick the first device_config.<name>.json if present
                deviceFileName = list.firstOrNull { it.startsWith("device_config.") && it.endsWith(".json") }
            }
            if (deviceFileName == null) return

            val jsonStr = am.open(deviceFileName).bufferedReader().use { it.readText() }
            val jo = org.json.JSONObject(jsonStr)

            val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
            val editor = prefs.edit()

            // Map known keys to preferences
            if (jo.has("preferred_ssid")) editor.putString("router_ssid_a", jo.getString("preferred_ssid"))
            if (jo.has("upload_ssid")) editor.putString("router_ssid_b", jo.getString("upload_ssid"))
            // 同一 APK 用: assets の terminal_id が空なら上書きしない（初回画面で設定）
            if (jo.has("terminal_id")) {
                val tid = jo.optString("terminal_id", "").trim()
                if (tid.isNotEmpty()) {
                    editor.putString("terminal_id", tid)
                    cachedTerminalId = tid
                }
            }

            // Store raw config for other components to read
            editor.putString("device_config_raw", jsonStr)

            editor.apply()
            RealTimeLogger.i("AppConfig", "Loaded $deviceFileName: ${jo.optString("terminal_id")}")
        } catch (e: Exception) {
            RealTimeLogger.w("AppConfig", "Failed to load device_config.json: ${e.message}")
        }
    }

    // --- Client Config (client_config.json) Support ---
    private var cachedTerminalId: String? = null
    private var cachedCurrentEdgeId: String? = null
    private var cachedTargetEdgeId: String? = null
    private var cachedCurrentRouterId: String? = null
    private var cachedTargetRouterId: String? = null

    fun getTerminalId(context: Context): String {
        if (cachedTerminalId != null) return cachedTerminalId!!
        // prefer value stored in SharedPreferences by loadDeviceConfig
        try {
            val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
            val tid = prefs.getString("terminal_id", null)
            if (!tid.isNullOrBlank()) {
                cachedTerminalId = tid
                return tid
            }
        } catch (_: Exception) {}

        loadClientConfig(context)
        return cachedTerminalId ?: "device-001"
    }

    fun getCurrentEdgeId(context: Context): String {
        if (cachedCurrentEdgeId != null) return cachedCurrentEdgeId!!
        loadClientConfig(context)
        return cachedCurrentEdgeId ?: "edge-default"
    }

    fun getTargetEdgeId(context: Context): String {
        if (cachedTargetEdgeId != null) return cachedTargetEdgeId!!
        loadClientConfig(context)
        return cachedTargetEdgeId ?: getCurrentEdgeId(context)
    }

    fun getCurrentRouterId(context: Context): String {
        if (cachedCurrentRouterId != null) return cachedCurrentRouterId!!
        loadClientConfig(context)
        return cachedCurrentRouterId ?: "router-default"
    }

    fun getTargetRouterId(context: Context): String {
        if (cachedTargetRouterId != null) return cachedTargetRouterId!!
        loadClientConfig(context)
        return cachedTargetRouterId ?: getCurrentRouterId(context)
    }

    private fun loadClientConfig(context: Context) {
        try {
            val file = java.io.File(context.filesDir, "client_config.json")
            if (file.exists()) {
                val json = org.json.JSONObject(file.readText())
                cachedTerminalId = json.optString("terminal_id").takeIf { it.isNotEmpty() }
                cachedCurrentEdgeId = json.optString("current_edge_id").takeIf { it.isNotEmpty() }
                cachedTargetEdgeId = json.optString("target_edge_id").takeIf { it.isNotEmpty() }
                cachedCurrentRouterId = json.optString("current_router_id").takeIf { it.isNotEmpty() }
                cachedTargetRouterId = json.optString("target_router_id").takeIf { it.isNotEmpty() }
            }
        } catch (e: Exception) {
            // ignore
        }
    }

    // Allow runtime override to prefer query-style download URLs (e.g. /download?rel_path=...)
    // Store boolean in PREFS_ENV as "prefer_query_first" (default: false)
    fun getPreferQueryFirst(context: Context): Boolean {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        return prefs.getBoolean("prefer_query_first", false)
    }
}
