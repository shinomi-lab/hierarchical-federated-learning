// com.example.hfl_experiment.network.util.PingUtil.kt
package com.example.hfl_experiment.network.util

import android.content.Context
import android.net.wifi.WifiManager
import com.example.hfl_experiment.util.logging.RealTimeLogger
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.lang.Exception

object PingUtil {
    private const val TAG = "NetworkPingUtil"

    suspend fun pingGateway(context: Context): Float? = withContext(Dispatchers.IO) {
        try {
            val wifiManager = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val dhcpInfo = wifiManager.dhcpInfo
            val gatewayInt = dhcpInfo.gateway
            val gatewayIp = String.format(
                "%d.%d.%d.%d",
                (gatewayInt and 0xff),
                (gatewayInt shr 8 and 0xff),
                (gatewayInt shr 16 and 0xff),
                (gatewayInt shr 24 and 0xff)
            )

            val start = System.currentTimeMillis()
            val process = ProcessBuilder("ping", "-c", "1", gatewayIp).start()
            val result = process.waitFor()
            val end = System.currentTimeMillis()

            val rtt = if (result == 0) (end - start).toFloat() else null
            try { if (rtt != null) RealTimeLogger.i(TAG, "pingGateway: gatewayIp=$gatewayIp rtt=${rtt}ms") } catch (_: Exception) {}
            return@withContext rtt
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "pingGateway failed: ${e.message}")
            return@withContext null
        }
    }
}
