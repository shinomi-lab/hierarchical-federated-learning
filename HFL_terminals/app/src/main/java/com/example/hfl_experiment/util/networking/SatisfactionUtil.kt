package com.example.hfl_experiment.util.networking

/**
 * 端末満足度 S_i の計算ユーティリティ。
 * G_TP: S = TP_link / TP_need
 * G_RTT: S = RTT_need / RTT_link
 * 値域は 0 < S <= 1 にクリップ（必要値が0やリンク値が0の境界にも安全）。
 */
object SatisfactionUtil {
    enum class AppGroup { G_TP, G_RTT }

    /**
     * @param group アプリのグループ（TP重視 or RTT重視）
     * @param linkValue TP_link もしくは RTT_link（測定値）
     * @param needValue TP_need もしくは RTT_need（要求値）
     * @return 端末満足度 S_i（0<S<=1）。異常値は安全にクリップ。
     */
    fun compute(group: AppGroup, linkValue: Double, needValue: Double): Double {
        if (needValue <= 0.0) return 1.0 // 要求値ゼロは常に満足とみなす
        if (linkValue <= 0.0) return 0.0001 // 測定値ゼロは極小にする
        val raw = when (group) {
            AppGroup.G_TP -> linkValue / needValue
            AppGroup.G_RTT -> needValue / linkValue
        }
        // 0 < S <= 1 にクリップ
        val s = raw.coerceIn(0.0001, 1.0)
        return s
    }
}

