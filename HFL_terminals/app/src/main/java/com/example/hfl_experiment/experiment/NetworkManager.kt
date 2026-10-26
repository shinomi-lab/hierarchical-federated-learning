package com.example.hfl_experiment.experiment

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiManager
import android.net.wifi.WifiNetworkSpecifier
import android.os.Build
import android.util.Log
import androidx.annotation.RequiresApi
import com.example.hfl_experiment.util.networking.PingUtil
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/**
 * ApConfig simplified for NetworkManager usage
 */
data class ApConfig(val id: String, val ssid: String, val psk: String)

/**
 * NetworkManager: manages Wi-Fi AP connections and reports real network quality metrics.
 * Uses WifiNetworkSpecifier (API 29+) to request a specific SSID.
 * Note: on first use, Android may show a system dialog asking the user to confirm the connection.
 */
class NetworkManager(private val context: Context) {
    private val TAG = "NetworkManager"
    private var currentAp: ApConfig? = null
    private var currentNetwork: Network? = null
    private var activeCallback: ConnectivityManager.NetworkCallback? = null

    suspend fun connectTo(ap: ApConfig): Boolean = withContext(Dispatchers.IO) {
        releaseCurrentCallback()

        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            Log.w(TAG, "connectTo: WifiNetworkSpecifier requires API 29+; current API=${Build.VERSION.SDK_INT}")
            return@withContext false
        }

        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val specBuilder = WifiNetworkSpecifier.Builder().setSsid(ap.ssid)
        if (ap.psk.isNotBlank()) {
            specBuilder.setWpa2Passphrase(ap.psk)
        }
        val specifier = specBuilder.build()

        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .setNetworkSpecifier(specifier)
            .build()

        val deferred = CompletableDeferred<Boolean>()
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                currentNetwork = network
                currentAp = ap
                Log.i(TAG, "Connected to ${ap.ssid}")
                if (!deferred.isCompleted) deferred.complete(true)
            }
            override fun onUnavailable() {
                Log.w(TAG, "Network unavailable for ${ap.ssid}")
                if (!deferred.isCompleted) deferred.complete(false)
            }
            override fun onLost(network: Network) {
                Log.w(TAG, "Network lost for ${ap.ssid}")
                if (currentNetwork == network) {
                    currentNetwork = null
                    currentAp = null
                }
            }
        }

        return@withContext try {
            cm.requestNetwork(request, callback)
            activeCallback = callback
            val result = withTimeoutOrNull(15_000L) { deferred.await() } ?: run {
                Log.w(TAG, "connectTo: timed out waiting for ${ap.ssid}")
                false
            }
            if (!result) releaseCurrentCallback()
            result
        } catch (e: Exception) {
            Log.w(TAG, "connectTo failed: ${e.message}")
            releaseCurrentCallback()
            false
        }
    }

    suspend fun disconnectCurrent(): Boolean {
        return try {
            currentAp?.let { Log.i(TAG, "Disconnecting from ${it.ssid}") }
            releaseCurrentCallback()
            currentAp = null
            currentNetwork = null
            true
        } catch (e: Exception) {
            Log.w(TAG, "disconnectCurrent failed: ${e.message}")
            false
        }
    }

    /**
     * Returns (throughput_Mbps, rtt_ms) using real measurements.
     * Throughput comes from WifiManager link speed; RTT from ICMP ping to the gateway.
     */
    fun getCurrentQuality(): Pair<Float, Float> {
        if (currentAp == null) return Pair(0f, 9999f)

        val wifiManager = context.applicationContext
            .getSystemService(Context.WIFI_SERVICE) as? WifiManager
        val linkSpeedMbps = wifiManager?.connectionInfo?.linkSpeed
            ?.toFloat()?.takeIf { it > 0 } ?: 0f

        val rttMs = try {
            // Use LinkProperties of the bound network for accurate gateway IP
            val net = currentNetwork
            if (net != null) {
                val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
                val gwIp = cm.getLinkProperties(net)
                    ?.routes
                    ?.mapNotNull { it.gateway }
                    ?.firstOrNull { !it.isAnyLocalAddress }
                    ?.hostAddress
                if (gwIp != null) pingHost(gwIp) else PingUtil.pingGateway(context)?.toFloat()
            } else {
                PingUtil.pingGateway(context)?.toFloat()
            }
        } catch (e: Exception) {
            Log.w(TAG, "getCurrentQuality RTT measurement failed: ${e.message}")
            PingUtil.pingGateway(context)?.toFloat()
        } ?: 9999f

        return Pair(linkSpeedMbps, rttMs)
    }

    fun getCurrentApId(): String? = currentAp?.id

    private fun releaseCurrentCallback() {
        try {
            activeCallback?.let {
                val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
                cm.unregisterNetworkCallback(it)
            }
        } catch (_: Exception) {}
        activeCallback = null
    }

    /** Pings the given host IP and returns RTT in ms, or null on failure. */
    private fun pingHost(host: String): Float? {
        return try {
            val process = Runtime.getRuntime().exec(arrayOf("ping", "-c", "1", "-w", "1", host))
            val output = process.inputStream.bufferedReader().readText()
            process.waitFor()
            process.destroy()
            val timePart = output.lines()
                .firstOrNull { it.contains("time=") }
                ?.substringAfter("time=")
                ?.substringBefore(" ")
            timePart?.toFloatOrNull()
        } catch (e: Exception) {
            Log.w(TAG, "pingHost($host) failed: ${e.message}")
            null
        }
    }
}

