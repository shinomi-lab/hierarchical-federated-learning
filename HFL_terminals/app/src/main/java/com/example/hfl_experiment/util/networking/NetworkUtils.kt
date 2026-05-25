package com.example.hfl_experiment.util.networking

import android.content.Context
import android.net.wifi.WifiManager
import com.example.hfl_experiment.AppConfig
import com.example.hfl_experiment.util.logging.RealTimeLogger
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Small networking helpers used by measurement and decision logic.
 * - getCurrentSsid: best-effort SSID discovery (assumes location permission granted)
 * - getTargetUrl: returns edge base URL + path (port matches SSID via [AppConfig.getEdgeBaseUrl])
 * - measureHttpRtt: simple HTTP GET based RTT measurement (suspend, IO dispatcher)
 */
object NetworkUtils {
    private const val TAG = "NetworkUtils"

    /** Best-effort SSID extraction. Returns unquoted SSID or null. */
    fun getCurrentSsid(context: Context): String? {
        return try {
            val wm = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val info = wm.connectionInfo
            val raw = info?.ssid
            if (raw.isNullOrBlank()) return null
            // remove surrounding quotes when present
            raw.trim('"')
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "getCurrentSsid failed: ${e.message}")
            null
        }
    }

    /**
     * Build a target URL to hit for RTT/availability checks.
     * Port follows [AppConfig.getEdgeBaseUrl] (SSID-aware 8001 / 8002).
     * path should start with '/'
     */
    fun getTargetUrl(context: Context, path: String = "/ping"): String {
        val currentSsid = getCurrentSsid(context)
        val primary = AppConfig.getPrimarySsid(context)
        val secondary = AppConfig.getSecondarySsid(context)
        val base = AppConfig.getEdgeBaseUrl(context).trimEnd('/')
        val p = if (path.startsWith("/")) path else "/$path"
        val result = base + p
        RealTimeLogger.i(TAG, "getTargetUrl: ssid=$currentSsid primary=$primary secondary=$secondary url=$result")
        return result
    }

    /**
     * Measure a simple HTTP GET RTT (ms) to given absolute URL. Returns elapsed ms on success.
     * Throws IOException on failure.
     */
    suspend fun measureHttpRtt(url: String, timeoutSeconds: Long = 10): Long = withContext(Dispatchers.IO) {
        val client = OkHttpClient.Builder()
            .connectTimeout(timeoutSeconds, TimeUnit.SECONDS)
            .readTimeout(timeoutSeconds, TimeUnit.SECONDS)
            .callTimeout(timeoutSeconds + 5, TimeUnit.SECONDS)
            .build()

        val req = Request.Builder().url(url).get().build()
        val start = System.currentTimeMillis()
        client.newCall(req).execute().use { resp ->
            val elapsed = System.currentTimeMillis() - start
            if (!resp.isSuccessful) {
                RealTimeLogger.w(TAG, "measureHttpRtt: non-success code=${resp.code} url=$url")
                throw IOException("HTTP ${resp.code} for $url")
            }
            RealTimeLogger.i(TAG, "measureHttpRtt: success elapsed=${elapsed}ms url=$url")
            elapsed
        }
    }

    /** Convenience wrapper to pick target and measure RTT (returns null on failure). */
    suspend fun measureRttForCurrentNetwork(context: Context, path: String = "/ping"): Long? {
        val url = getTargetUrl(context, path)
        return try {
            val rtt = measureHttpRtt(url)
            RealTimeLogger.i(TAG, "measureRttForCurrentNetwork: rtt=${rtt}ms url=$url")
            rtt
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "measureRttForCurrentNetwork: failed url=$url err=${e.message}")
            null
        }
    }
}

