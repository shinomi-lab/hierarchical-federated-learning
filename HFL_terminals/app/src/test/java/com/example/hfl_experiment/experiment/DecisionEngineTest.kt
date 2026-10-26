package com.example.hfl_experiment.experiment

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.example.hfl_experiment.AppConfig
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config as RoboConfig

// 旧版は mockito の Context と ap_config.json のファイル直読みで JVM 上を走らせようとしていたが、
// 実際に落ちていた原因は Context ではなく Config.ConfigData.apNum が 0 になること（SP3d 0.1 で修正、下のテスト参照）。
// Robolectric の実 Context で assets から読み込み、本番と同じ経路の設定で検証する。
@RunWith(RobolectricTestRunner::class)
@RoboConfig(sdk = [34])
class DecisionEngineTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private lateinit var engine: DecisionEngine
    private lateinit var configData: Config.ConfigData

    @Before
    fun setup() {
        // AppConfig は object（JVM 内で共有）なので、モデル推論経路（baseline_mode=none）に固定する
        ctx.getSharedPreferences("env_prefs", Context.MODE_PRIVATE).edit().clear().commit()
        AppConfig.updateServerBaselineMode(null)
        AppConfig.endSessionBaselineMode()
        AppConfig.loadBaselineMode(ctx)
        assertEquals("none", AppConfig.BASELINE_MODE)

        configData = Config.loadConfigFromAssets(ctx)
        engine = DecisionEngine(ctx, configData)
    }

    @After
    fun tearDown() {
        AppConfig.endSessionBaselineMode()
        AppConfig.updateServerBaselineMode(null)
    }

    @Test
    fun testEmaSmoothingAndConsecutiveThreshold() {
        val appOneHot = floatArrayOf(1f, 0f, 0f, 0f)
        // feed repeated observations that favor AP1
        var res = engine.observe(tp = 30f, rtt = 50f, appOneHot = appOneHot)
        assertNotNull(res)
        // call multiple times to exceed consecutiveThreshold
        for (i in 0 until (configData.consecutiveThreshold + 1)) {
            res = engine.observe(tp = 100f, rtt = 20f, appOneHot = appOneHot)
        }
        assertTrue(res.chosenApId.isNotBlank())
        assertEquals(configData.apNum, res.emaScores.size)
    }

    @Test
    fun testDebounceMinSwitch() {
        val appOneHot = floatArrayOf(0f, 1f, 0f, 0f)
        val res1 = engine.observe(tp = 50f, rtt = 30f, appOneHot = appOneHot)
        val res2 = engine.observe(tp = 200f, rtt = 10f, appOneHot = appOneHot)
        assertNotNull(res1)
        assertNotNull(res2)
    }

    // SP3d 0.1: ConfigData.apNum はクラス本体の初期化子 `val apNum: Int = 2` だったため、Gson（Unsafe で生成し
    // 初期化子を走らせない）で読むと 0 になり、observe() が none では空配列の添字、fixed/greedy では coerceIn(0, -1)、
    // random では空範囲の random() で例外になっていた（呼び出し側で握り潰され、ベースラインは AP を一度も切り替えなかった）。
    @Test
    fun loadConfigFromAssets_apNumIsTwo() {
        val raw = Config.loadConfigFromAssets(ctx)
        assertEquals(2, raw.apNum)
        val res = DecisionEngine(ctx, raw).observe(tp = 50f, rtt = 30f, appOneHot = floatArrayOf(1f, 0f, 0f, 0f))
        assertEquals(2, res.emaScores.size)
    }

    @Test
    fun loadConfigFromAssets_baselineModesChooseAnAp() {
        val raw = Config.loadConfigFromAssets(ctx)
        try {
            for (mode in listOf("fixed", "random", "greedy")) {
                AppConfig.setBaselineMode(ctx, mode)
                assertEquals(mode, AppConfig.BASELINE_MODE)
                val res = DecisionEngine(ctx, raw).observe(tp = 50f, rtt = 30f, appOneHot = floatArrayOf(0f, 1f, 0f, 0f))
                assertTrue("mode=$mode chosen=${res.chosenApId}", res.chosenApId in setOf(raw.apA.id, raw.apB.id))
                assertEquals(2, res.emaScores.size)
            }
        } finally {
            AppConfig.setBaselineMode(ctx, "none")
        }
    }

    // --- centralized-mode: central では端末内モデルを使わず、失敗しても AP を変えない ---
    private fun centralEngine(): Pair<DecisionEngine, ModelLoader> {
        AppConfig.setBaselineMode(ctx, "central")
        assertEquals("central", AppConfig.BASELINE_MODE)
        val loader = org.mockito.Mockito.mock(ModelLoader::class.java)
        return DecisionEngine(ctx, configData, modelLoader = loader) to loader
    }

    private fun preferredId(): String =
        configData.getApIdByIndex(configData.getLastSelectedIndexSafe(ctx) ?: 0)

    @Test
    fun central_observe_neverUsesOnDeviceModel() {
        val (e, loader) = centralEngine()
        val r = e.observe(tp = 50f, rtt = 10f, appOneHot = floatArrayOf(0f, 1f, 0f, 0f))
        org.mockito.Mockito.verify(loader, org.mockito.Mockito.never()).predict(org.mockito.kotlin.any())
        assertEquals("baseline_central", r.reason)
        assertEquals(preferredId(), r.chosenApId)   // 実測では AP を変えない
    }

    @Test
    fun central_ok_returnsRecommendedAp_withoutOnDeviceModel() {
        val (e, loader) = centralEngine()
        for (idx in 0 until configData.apNum) {
            val r = e.observeCentral(CentralOutcome(CentralClient.STATUS_OK, 12, chosenAp = idx, scores = listOf(0.4, 0.6), modelSha256 = "x"))
            assertEquals(configData.getApIdByIndex(idx), r.chosenApId)
            assertEquals("central_ok", r.reason)
            assertEquals(listOf(0.4f, 0.6f), r.rawScores)
        }
        org.mockito.Mockito.verify(loader, org.mockito.Mockito.never()).predict(org.mockito.kotlin.any())
    }

    @Test
    fun central_failure_keepsCurrentAp_forEveryFailureKind() {
        val (e, loader) = centralEngine()
        val before = preferredId()
        for (st in listOf(CentralClient.STATUS_TIMEOUT, CentralClient.STATUS_UNREACHABLE, CentralClient.STATUS_SERVER_ERROR,
            CentralClient.STATUS_BAD_REQUEST, CentralClient.STATUS_BAD_RESPONSE, CentralClient.STATUS_NOT_CONFIGURED)) {
            val r = e.observeCentral(CentralOutcome(st, 5000))
            assertEquals("status=$st", before, r.chosenApId)
            assertEquals("central_$st", r.reason)
        }
        org.mockito.Mockito.verify(loader, org.mockito.Mockito.never()).predict(org.mockito.kotlin.any())
        assertEquals(before, preferredId())   // 保存している「選択中の AP」も変えていない
    }

    @Test
    fun central_outOfRangeRecommendation_isClampedToKnownAp() {
        val (e, _) = centralEngine()
        val r = e.observeCentral(CentralOutcome(CentralClient.STATUS_OK, 3, chosenAp = 7, scores = listOf(1.0), modelSha256 = "x"))
        assertEquals(configData.getApIdByIndex(configData.apNum - 1), r.chosenApId)
    }

    // --- fix-audit-20261026: fixed_ap1・Random をセッションの最初に 1 回だけ決める ---
    private fun observeIdx(e: DecisionEngine): String = e.observe(tp = 50f, rtt = 30f, appOneHot = floatArrayOf(0f, 1f, 0f, 0f)).chosenApId

    @Test
    fun fixedAp1_alwaysChoosesAp1_andFixedStaysAp0() {
        try {
            AppConfig.setBaselineMode(ctx, "fixed_ap1")
            val e = DecisionEngine(ctx, configData)
            repeat(5) { assertEquals(configData.apB.id, observeIdx(e)) }
            AppConfig.setBaselineMode(ctx, "fixed")
            repeat(3) { assertEquals(configData.apA.id, observeIdx(DecisionEngine(ctx, configData))) }
        } finally {
            AppConfig.setBaselineMode(ctx, "none")
        }
    }

    @Test
    fun random_keepsFirstChoiceWithinSession_andRedrawsAfterReset() {
        try {
            AppConfig.setBaselineMode(ctx, "random")
            val e = DecisionEngine(ctx, configData, random = kotlin.random.Random(42))
            val seen = mutableSetOf<String>()
            repeat(20) {
                e.resetSessionRandomChoice()
                assertEquals(null, e.sessionRandomChoice)
                val first = observeIdx(e)
                repeat(9) { assertEquals(first, observeIdx(e)) }   // セッション内（計測 10 回）は同じ
                assertEquals(if (first == configData.apA.id) 0 else 1, e.sessionRandomChoice)
                seen.add(first)
            }
            assertEquals(setOf(configData.apA.id, configData.apB.id), seen)   // 選び直すと両方出る
        } finally {
            AppConfig.setBaselineMode(ctx, "none")
        }
    }
}
