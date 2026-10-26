package com.example.hfl_experiment.util.networking

import okhttp3.OkHttpClient
import okhttp3.Request
import com.example.hfl_experiment.util.logging.RealTimeLogger

object NetworkBandwidthProbe {
    private const val TAG = "BandwidthProbe"

    /** メトリクスの tp_measured_mbps の単位（行の tp_unit に入れる）。修正前の行にはこの項目が無く、値は MB/s。 */
    const val TP_UNIT = "Mbps"

    /**
     * 受信バイト数と経過時間 → Mbps（10^6 bit/s）。満足度の要求値（Mbps）と同じ単位。
     * fix-audit-20261015 E7: 以前は bytes / 2^20 / 秒（MB/s、真の Mbps の約 1/8.4）を Mbps として返していた。
     */
    fun toMbps(bytes: Long, elapsedMs: Long): Double {
        val ms = elapsedMs.coerceAtLeast(1)
        return bytes.toDouble() * 8.0 / 1_000_000.0 / (ms / 1000.0)
    }

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
                val mbps = toMbps(readTotal, ms)
                RealTimeLogger.i(TAG, "measure: bytes=$readTotal ms=$ms mbps=${"%.2f".format(mbps)} url=$url")
                mbps
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "measure failed: ${e.message}")
            null
        }
    }
}
