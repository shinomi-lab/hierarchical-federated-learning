package com.example.hfl_experiment.telemetry

import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Policy ③ (c): DeviceMetricsCollector の状態管理ロジックを Robolectric で検証。
 *
 * 本日 newProto から取り込んだ telemetry のうち、計測値の集計・リセット動作が
 * TrainingViewModel 側の呼び出し順序に対して期待どおりかを確認する。
 * Android Context が必要なので Robolectric を使う。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33], manifest = Config.NONE)
class DeviceMetricsCollectorTest {

    @Before
    fun setup() {
        // テスト間でグローバル状態を初期化
        val ctx = ApplicationProvider.getApplicationContext<android.content.Context>()
        DeviceMetricsCollector.init(ctx)
        DeviceMetricsCollector.resetForSession(expectedEpochs = 25)
    }

    @Test
    fun dropoutEpochCount_isZero_whenAllExpectedEpochsCompleted() {
        DeviceMetricsCollector.resetForSession(expectedEpochs = 5)
        repeat(5) { DeviceMetricsCollector.recordEpochCompleted() }
        assertEquals(5, DeviceMetricsCollector.completedEpochCount)
        assertEquals(0, DeviceMetricsCollector.dropoutEpochCount)
    }

    @Test
    fun dropoutEpochCount_reflectsPartialCompletion() {
        DeviceMetricsCollector.resetForSession(expectedEpochs = 25)
        repeat(17) { DeviceMetricsCollector.recordEpochCompleted() }
        assertEquals(17, DeviceMetricsCollector.completedEpochCount)
        assertEquals(8, DeviceMetricsCollector.dropoutEpochCount)
    }

    @Test
    fun dropoutEpochCount_cannotGoNegative_evenIfOverCompleted() {
        DeviceMetricsCollector.resetForSession(expectedEpochs = 3)
        repeat(10) { DeviceMetricsCollector.recordEpochCompleted() }
        // expected < completed のとき dropout = 0 (負の値を返さない)
        assertEquals(0, DeviceMetricsCollector.dropoutEpochCount)
    }

    @Test
    fun networkIdleWaitMs_isNull_beforeAnyMarks() {
        DeviceMetricsCollector.resetForSession(expectedEpochs = 5)
        assertNull(DeviceMetricsCollector.networkIdleWaitMs)
    }

    @Test
    fun networkIdleWaitMs_returnsPositiveDiff_whenComputeFollowsNetwork() {
        DeviceMetricsCollector.resetForSession(expectedEpochs = 5)
        DeviceMetricsCollector.markNetworkComplete()
        Thread.sleep(50)
        DeviceMetricsCollector.markComputeStart()
        val wait = DeviceMetricsCollector.networkIdleWaitMs
        assertNotNull(wait)
        assertTrue("expected wait >= 40ms, got $wait", wait!! >= 40L)
    }

    @Test
    fun networkIdleWaitMs_isNull_whenComputeBeforeNetwork() {
        // 順序が逆 (compute_start < network_complete) のときは null
        DeviceMetricsCollector.lastNetworkCompleteTs = 0L
        DeviceMetricsCollector.lastComputeStartTs = 0L
        DeviceMetricsCollector.markComputeStart()
        Thread.sleep(10)
        DeviceMetricsCollector.markNetworkComplete()
        assertNull(DeviceMetricsCollector.networkIdleWaitMs)
    }

    @Test
    fun payloadVsTotalRatio_isComputedCorrectly() {
        DeviceMetricsCollector.resetForSession(expectedEpochs = 5)
        DeviceMetricsCollector.recordUploadTiming(payloadMs = 200L, totalMs = 1000L)
        val ratio = DeviceMetricsCollector.payloadVsTotalRatio
        assertNotNull(ratio)
        assertEquals(0.2, ratio!!, 1e-6)
    }

    @Test
    fun resetForSession_clearsCounters() {
        DeviceMetricsCollector.resetForSession(expectedEpochs = 10)
        repeat(7) { DeviceMetricsCollector.recordEpochCompleted() }
        assertEquals(7, DeviceMetricsCollector.completedEpochCount)

        // 新セッション開始でクリアされる
        DeviceMetricsCollector.resetForSession(expectedEpochs = 25)
        assertEquals(0, DeviceMetricsCollector.completedEpochCount)
        assertEquals(25, DeviceMetricsCollector.expectedEpochCount)
        assertEquals(25, DeviceMetricsCollector.dropoutEpochCount)   // まだ何も完了してない
    }
}
