package com.example.hfl_experiment.experiment

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.example.hfl_experiment.model.AppConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config as RoboConfig

@RunWith(RobolectricTestRunner::class)
@RoboConfig(sdk = [34])
class GreedyAppRequirementTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private val apps = listOf(
        AppConfig("ブラウザ", "tp", 0, 5, 5.0, 1.0), AppConfig("動画", "tp", 0, 10, 5.0, 1.0),
        AppConfig("通話", "rtt", 200, 0, 5.0, 1.0), AppConfig("配信", "rtt", 100, 0, 5.0, 1.0),
    )

    @Test fun requirement_comesFromAppJson() {
        val cfg = Config.loadConfigFromAssets(ctx)
        assertEquals(Triple("rtt", 0f, 100f), appRequirement(3, apps, cfg))
        assertEquals(Triple("tp", 10f, 0f), appRequirement(1, apps, cfg))
    }

    @Test fun greedy_streamingPicksLowRttAp() {
        val de = DecisionEngine(ctx, Config.loadConfigFromAssets(ctx), appConfigs = apps)
        de.apStatesForGreedy = mapOf(0 to floatArrayOf(50f, 300f, 1f), 1 to floatArrayOf(5f, 40f, 1f))  // AP0: 高TP高RTT, AP1: 低RTT
        assertEquals(1, de.pickGreedyForTest(floatArrayOf(0f, 0f, 0f, 1f)))   // 配信 → RTT の良い AP1
        assertEquals(0, de.pickGreedyForTest(floatArrayOf(0f, 1f, 0f, 0f)))   // 動画 → TP の良い AP0
    }
}

/** record-connected-ap 3.1: Greedy は実験で使う AP（apA・apB）だけを、実測優先（無ければ模擬値）で比べ、出所を残す。 */
@RunWith(RobolectricTestRunner::class)
@RoboConfig(sdk = [34])
class GreedyMeasuredFirstTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private val apps = listOf(
        AppConfig("ブラウザ", "tp", 0, 5, 5.0, 1.0), AppConfig("動画", "tp", 0, 10, 5.0, 1.0),
        AppConfig("通話", "rtt", 200, 0, 5.0, 1.0), AppConfig("配信", "rtt", 100, 0, 5.0, 1.0),
    )
    private val video = floatArrayOf(0f, 1f, 0f, 0f)   // 要求 TP 10 Mbps

    private fun engine() = DecisionEngine(ctx, Config.loadConfigFromAssets(ctx), appConfigs = apps)

    @Test fun measuredBeatsModel_modelWouldHaveSentVideoToB() {
        val de = engine()
        // 模擬値だけなら B（40 Mbps）が良く見えるが、実測では B は 5.9 Mbps で要求 10 Mbps を満たさない
        de.apStateInfosForGreedy = mapOf(
            0 to ApStateInfo(tp = 11.7f, rtt = 3f, n = 1f, measuredTp = 11.0f, measuredRtt = 3f),
            1 to ApStateInfo(tp = 40f, rtt = 100f, n = 1f, measuredTp = 5.9f, measuredRtt = 7f),
        )
        assertEquals(0, de.pickGreedyForTest(video))
        assertEquals(mapOf(0 to "measured", 1 to "measured"), de.lastGreedySources)
    }

    @Test fun fallsBackToModelWhenNoMeasurement() {
        val de = engine()
        de.apStateInfosForGreedy = mapOf(
            0 to ApStateInfo(tp = 11.7f, rtt = 3f, n = 1f, measuredTp = 4f, measuredRtt = 3f),   // A の実測は悪い
            1 to ApStateInfo(tp = 20f, rtt = 7f, n = 0f),                                     // B は実測なし → 模擬値 20 Mbps
        )
        assertEquals(1, de.pickGreedyForTest(video))
        assertEquals(mapOf(0 to "measured", 1 to "model"), de.lastGreedySources)
    }

    @Test fun ignoresApsOutsideTheConfiguredPair() {
        val de = engine()
        // サーバが旧形式で AP2 を返しても、比べるのは apA・apB（AP 番号 0・1）だけ。AP2 が最良でも選ばない
        de.apStateInfosForGreedy = mapOf(
            0 to ApStateInfo(tp = 3f, rtt = 3f, n = 1f),
            1 to ApStateInfo(tp = 4f, rtt = 7f, n = 1f),
            2 to ApStateInfo(tp = 500f, rtt = 1f, n = 0f),
        )
        val picked = de.pickGreedyForTest(video)
        assertTrue("picked=$picked", picked in 0..1)
        assertEquals(setOf(0, 1), de.lastGreedySources.keys)
    }

    @Test fun oldFormatArrayStatesStillWork() {
        val de = engine()
        de.apStatesForGreedy = mapOf(0 to floatArrayOf(50f, 300f, 1f), 1 to floatArrayOf(5f, 40f, 1f))
        assertEquals(1, de.pickGreedyForTest(floatArrayOf(0f, 0f, 0f, 1f)))
        assertEquals(mapOf(0 to "model", 1 to "model"), de.lastGreedySources)
    }

    @Test fun partialMeasurementUsesMeasuredForThatMetricOnly() {
        val info = ApStateInfo(tp = 40f, rtt = 100f, n = 1f, measuredTp = 6f, measuredRtt = null)
        val e = effectiveApState(info)
        assertEquals(6f, e.tp, 0f); assertEquals(100f, e.rtt, 0f); assertEquals("measured", e.source)
        val m = effectiveApState(ApStateInfo(tp = 40f, rtt = 100f, n = 0f))
        assertEquals(40f, m.tp, 0f); assertEquals("model", m.source)
    }

    @Test fun noStatesDefaultsToAp0() {
        assertEquals(0, engine().pickGreedyForTest(video))
    }
}
