package com.example.hfl_experiment.util.networking

import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiNetworkSuggestion
import android.os.Build
import android.provider.Settings
import com.example.hfl_experiment.util.logging.RealTimeLogger

/**
 * 半強制的なネットワーク切替（提案＋接続監視）を行うための最小ヘルパー。
 * 強制接続はできないが、Suggestion登録によりユーザー承認のもとで接続される可能性が高まる。
 */
object NetworkSwitchHelper {
    private const val TAG = "NetworkSwitchHelper"

    /** 指定SSIDへの接続を提案（必要ならWPA2パスフレーズも指定）。API 29+ は Suggestion。未満は Wi‑Fi 設定画面へ誘導。戻り値は 0=成功っぽい、負値=フォールバック/失敗。 */
    fun suggestWifi(context: Context, ssid: String, passphrase: String? = null): Int {
        return try {
            if (Build.VERSION.SDK_INT >= 29) {
                val builder = WifiNetworkSuggestion.Builder()
                    .setSsid(ssid)
                    .setIsAppInteractionRequired(true)
                if (!passphrase.isNullOrBlank()) builder.setWpa2Passphrase(passphrase)
                val suggestion = builder.build()
                val wifiManager = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as android.net.wifi.WifiManager
                val status = wifiManager.addNetworkSuggestions(listOf(suggestion))
                RealTimeLogger.i(TAG, "addNetworkSuggestions ssid=$ssid status=$status")
                status // 0 なら SUCCESS
            } else {
                // API 28 以下: Suggestion 不可。Settings.Panel は API 29+ のため Wi‑Fi 設定画面へ誘導する。
                try {
                    val intent = Intent(Settings.ACTION_WIFI_SETTINGS)
                    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    context.startActivity(intent)
                    RealTimeLogger.i(TAG, "Open Wi‑Fi settings for ssid=$ssid (API<29)")
                    -2 // フォールバックコード
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "open Wi‑Fi settings failed: ${e.message}")
                    -3
                }
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "suggestWifi failed: ${e.message}")
            -1
        }
    }

    /** 現在Wi‑Fiで、指定SSIDに接続しているかを判定。 */
    fun isConnectedToSsid(context: Context, targetSsid: String): Boolean {
        return try {
            val wifiManager = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as android.net.wifi.WifiManager
            val info = wifiManager.connectionInfo
            val current = info?.ssid?.trim('"')
            current != null && current == targetSsid
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "isConnectedToSsid failed: ${e.message}")
            false
        }
    }

    /** Wi‑Fi接続成立を簡易監視（タイムアウトmsまで）。成立したらtrue。 */
    fun awaitWifiConnected(context: Context, timeoutMs: Long = 15_000L, onUpdate: ((String?) -> Unit)? = null): Boolean {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val req = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .build()
        var connected = false
        val lock = Object()
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                try {
                    val active = cm.getNetworkCapabilities(network)
                    if (active?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true) {
                        connected = true
                        onUpdate?.invoke(getCurrentSsid(context))
                        synchronized(lock) { lock.notifyAll() }
                    }
                } catch (_: Exception) { }
            }
        }
        try { cm.registerNetworkCallback(req, cb) } catch (e: Exception) { RealTimeLogger.w(TAG, "registerNetworkCallback failed: ${e.message}") }
        val start = System.currentTimeMillis()
        synchronized(lock) {
            while (!connected && System.currentTimeMillis() - start < timeoutMs) {
                try { lock.wait(500) } catch (_: InterruptedException) { }
            }
        }
        try { cm.unregisterNetworkCallback(cb) } catch (_: Exception) { }
        return connected
    }

    private fun getCurrentSsid(context: Context): String? {
        return try {
            val wifiManager = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as android.net.wifi.WifiManager
            wifiManager.connectionInfo?.ssid?.trim('"')
        } catch (_: Exception) { null }
    }

    /** Public accessor for current SSID (wrapper around private getCurrentSsid). */
    fun getConnectedSsid(context: Context): String? {
        return try { getCurrentSsid(context) } catch (e: Exception) { RealTimeLogger.w(TAG, "getConnectedSsid failed: ${e.message}"); null }
    }

    /** Open the system Wi‑Fi settings panel (best-effort). Returns 0 on success, negative on failure. */
    fun openWifiSettings(context: Context): Int {
        return try {
            val intent = if (Build.VERSION.SDK_INT >= 29) {
                Intent(Settings.Panel.ACTION_WIFI)
            } else {
                Intent(Settings.ACTION_WIFI_SETTINGS)
            }
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(intent)
            RealTimeLogger.i(TAG, "Open Wi‑Fi settings")
            0
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "openWifiSettings failed: ${e.message}")
            -1
        }
    }
}
