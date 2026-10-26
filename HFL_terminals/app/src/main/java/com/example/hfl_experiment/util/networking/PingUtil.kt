package com.example.hfl_experiment.util.networking

import android.content.Context
import android.net.ConnectivityManager
import com.example.hfl_experiment.util.logging.RealTimeLogger
import java.io.BufferedReader
import java.io.InputStreamReader

object PingUtil {
    private const val TAG = "PingUtil"

    /**
     * デフォルトゲートウェイ（取得不可なら8.8.8.8）にPingを打ち、RTT(ms)を返す。
     * 失敗時はnull。
     */
    fun pingGateway(context: Context): Long? {
        val targetIp = getGatewayIp(context) ?: "8.8.8.8"
        return executePing(targetIp)
    }

    private fun getGatewayIp(context: Context): String? {
        return try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            val net = cm?.activeNetwork ?: return null
            val lp = cm.getLinkProperties(net) ?: return null
            // ルート情報からゲートウェイを探す（0.0.0.0/0 または ::/0 のネクストホップ）
            for (route in lp.routes) {
                val gw = route.gateway
                if (gw != null && !gw.isAnyLocalAddress) {
                    return gw.hostAddress
                }
            }
            null
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "getGatewayIp failed: ${e.message}")
            null
        }
    }

    private fun executePing(host: String): Long? {
        return try {
            // -c 1: count 1, -w 1: deadline 1 sec
            val cmd = "ping -c 1 -w 1 $host"
            val process = Runtime.getRuntime().exec(cmd)
            val reader = BufferedReader(InputStreamReader(process.inputStream))
            var line: String?
            var rtt: Long? = null

            while (reader.readLine().also { line = it } != null) {
                if (line?.contains("time=") == true) {
                    // extract "time=12.3 ms" -> 12.3
                    val parts = line.split("time=")
                    if (parts.size > 1) {
                        val timePart = parts[1].split(" ")[0]
                        rtt = timePart.toDoubleOrNull()?.toLong()
                        try { RealTimeLogger.i(TAG, "executePing: host=$host rtt=${rtt}ms") } catch (_: Exception) {}
                    }
                }
            }
            process.waitFor()
            rtt
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "executePing failed: ${e.message}")
            null
        }
    }
}
