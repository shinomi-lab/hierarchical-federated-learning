package com.example.hfl_experiment.training.optim

// package com.example.hfl_experiment.training.optim

/**
 * 学習率スケジューラ：ウォームアップ + コサイン減衰（最小 lr 比率指定）
 */
class WarmupCosineScheduler(
    private val groups: List<ParamGroup>,
    private val baseLr: Float,
    private val minLrRatio: Float = 0.1f,
    private val warmupSteps: Int = 0,
    private val totalSteps: Int
) {
    private var stepCount = 0

    fun step() {
        stepCount += 1
        val lr = currentLr()
        for (g in groups) g.lr = lr
    }

    fun currentLr(): Float {
        if (stepCount <= warmupSteps && warmupSteps > 0) {
            // 線形ウォームアップ
            return baseLr * (stepCount.toFloat() / warmupSteps)
        }
        val t = (stepCount - warmupSteps).coerceAtLeast(0)
        val T = (totalSteps - warmupSteps).coerceAtLeast(1)
        val cosine = 0.5f * (1f + kotlin.math.cos(Math.PI * t / T).toFloat())
        val minLr = baseLr * minLrRatio
        return minLr + (baseLr - minLr) * cosine
    }
}
