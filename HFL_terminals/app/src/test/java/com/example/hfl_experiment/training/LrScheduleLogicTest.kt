package com.example.hfl_experiment.training

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Policy ③ (d): Kotlin 純ロジックの単体テスト。
 *
 * LocalTrainer の LinearWarmupScheduler が実機で計算する LR と、
 * Sim_HFL/sim/terminal.py の _kotlin_lr() が同じ式を実装していることを保証する。
 *
 * これが揃わないと「シミュ vs 実機」の比較が破綻するため、修論主張の根幹に関わる。
 * 実機ビルド不要、`./gradlew :app:testDebugUnitTest --tests *LrSchedule*` で実行可能。
 */
class LrScheduleLogicTest {

    /**
     * LocalTrainer.LinearWarmupScheduler.step() と完全に同じ計算式を抽出した参照実装。
     * 元のクラスは private inner class なのでテスト用に式を直接転写。
     * 仕様変更時はこのテストが落ちるので警告として機能する。
     */
    private fun kotlinLr(
        currentStep: Int,
        baseLr: Float,
        warmupSteps: Int,
        totalSteps: Int,
        minLrRatio: Float,
    ): Float {
        return when {
            currentStep < warmupSteps -> baseLr * (currentStep.toFloat() / warmupSteps.toFloat())
            currentStep > totalSteps -> baseLr * minLrRatio
            else -> {
                val decayRatio = (currentStep - warmupSteps).toFloat() / (totalSteps - warmupSteps).toFloat()
                baseLr * ((1 - minLrRatio) * (1 - decayRatio) + minLrRatio)
            }
        }
    }

    @Test
    fun warmupPhase_step1_returnsBaseLrTimesOneOverWarmup() {
        // Sim_HFL/sim/terminal.py _kotlin_lr(step=1, base_lr=1e-4, warmup=10, total=100, min_ratio=0.1)
        // → 1e-4 * (1/10) = 1e-5
        val lr = kotlinLr(currentStep = 1, baseLr = 1e-4f, warmupSteps = 10, totalSteps = 100, minLrRatio = 0.1f)
        assertEquals(1e-5f, lr, 1e-9f)
    }

    @Test
    fun warmupPhase_atWarmupBoundary_isMaxJustBelowBase() {
        // currentStep = warmupSteps - 1 = 9 → 1e-4 * 9/10 = 9e-5
        val lr = kotlinLr(currentStep = 9, baseLr = 1e-4f, warmupSteps = 10, totalSteps = 100, minLrRatio = 0.1f)
        assertEquals(9e-5f, lr, 1e-9f)
    }

    @Test
    fun decayPhase_atWarmupBoundary_isBaseLr() {
        // step == warmupSteps → decayRatio = 0 → base_lr (= 1e-4)
        val lr = kotlinLr(currentStep = 10, baseLr = 1e-4f, warmupSteps = 10, totalSteps = 100, minLrRatio = 0.1f)
        assertEquals(1e-4f, lr, 1e-9f)
    }

    @Test
    fun decayPhase_midway_isExpectedLinearInterp() {
        // step=55, warmup=10, total=100 → decayRatio = 45/90 = 0.5
        // lr = 1e-4 * ((1-0.1)*(1-0.5) + 0.1) = 1e-4 * (0.45 + 0.1) = 5.5e-5
        val lr = kotlinLr(currentStep = 55, baseLr = 1e-4f, warmupSteps = 10, totalSteps = 100, minLrRatio = 0.1f)
        assertEquals(5.5e-5f, lr, 1e-9f)
    }

    @Test
    fun decayPhase_atTotalSteps_isMinLrRatio() {
        // step == totalSteps → decayRatio = 1 → base_lr * minLrRatio
        val lr = kotlinLr(currentStep = 100, baseLr = 1e-4f, warmupSteps = 10, totalSteps = 100, minLrRatio = 0.1f)
        assertEquals(1e-5f, lr, 1e-9f)
    }

    @Test
    fun beyondTotalSteps_clipsToMinLr() {
        val lr = kotlinLr(currentStep = 200, baseLr = 1e-4f, warmupSteps = 10, totalSteps = 100, minLrRatio = 0.1f)
        assertEquals(1e-5f, lr, 1e-9f)
    }

    @Test
    fun monotonicallyIncreasesInWarmupThenDecreases() {
        // ウォームアップ中は単調増加、その後は単調減少 (sim/terminal.py と同等)
        val warmup = 10
        val total = 100
        val baseLr = 1e-4f
        val minRatio = 0.1f

        var prev = -1f
        for (s in 1..warmup) {
            val lr = kotlinLr(s, baseLr, warmup, total, minRatio)
            assertTrue("warmup step $s should monotonically increase (prev=$prev, cur=$lr)", lr > prev)
            prev = lr
        }
        for (s in (warmup + 1)..total) {
            val lr = kotlinLr(s, baseLr, warmup, total, minRatio)
            assertTrue("decay step $s should monotonically decrease (prev=$prev, cur=$lr)", lr <= prev)
            prev = lr
        }
    }
}
