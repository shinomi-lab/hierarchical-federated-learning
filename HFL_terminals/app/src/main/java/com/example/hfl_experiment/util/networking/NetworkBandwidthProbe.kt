package com.example.hfl_experiment.util.networking

import okhttp3.OkHttpClient
import okhttp3.Request
import com.example.hfl_experiment.util.logging.RealTimeLogger

object NetworkBandwidthProbe {
    private const val TAG = "BandwidthProbe"

    /**
     * 指定URLをダウンロードして簡易TP(Mbps)を測定。成功ならMbpsを返す、失敗時はnull。
     * @param client 既存のOkHttpClientを使用
     * @param url 計測用の静的ファイルURL（数MB程度推奨）
     * @param maxBytes 最大読み取りバイト（例: 2MB）
     */
    fun measureMbps(client: OkHttpClient, url: String, maxBytes: Long = 2L * 1024 * 1024): Double? {
        return try {
            val req = Request.Builder().url(url).get().build()
            val start = System.currentTimeMillis()
            client.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return null
                val body = resp.body ?: return null
                val source = body.source()
                var readTotal = 0L
                val buf = okio.Buffer()
                while (readTotal < maxBytes) {
                    val read = source.read(buf, 8 * 1024)
                    if (read <= 0) break
                    readTotal += read
                }
                val end = System.currentTimeMillis()
                val ms = (end - start).coerceAtLeast(1)
                val mb = readTotal.toDouble() / (1024.0 * 1024.0)
                val mbps = (mb / (ms / 1000.0))
                RealTimeLogger.i(TAG, "measure: bytes=$readTotal ms=$ms mbps=${"%.2f".format(mbps)} url=$url")
                mbps
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "measure failed: ${e.message}")
            null
        }
    }
}
