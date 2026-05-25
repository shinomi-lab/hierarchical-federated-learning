package com.example.hfl_experiment.training.optim

// package com.example.hfl_experiment.training.optim

import kotlin.math.sqrt

object GradUtils {
    /**
     * 勾配の L2 ノルムでグローバルクリッピング（全 ParamRef を横断）
     * @param maxNorm しきい値（例: 1.0f）
     * @return 実際のグローバルノルム
     */
    fun clipGlobalL2(paramGroups: List<ParamGroup>, maxNorm: Float): Float {
        var sum = 0.0
        for (g in paramGroups) {
            for (p in g.params) {
                var i = 0
                while (i < p.length) {
                    val v = p.grad[i].toDouble()
                    sum += v * v
                    i++
                }
            }
        }
        val norm = sqrt(sum).toFloat()
        if (norm > maxNorm && norm > 0f) {
            val scale = maxNorm / norm
            for (g in paramGroups) {
                for (p in g.params) {
                    var i = 0
                    while (i < p.length) {
                        p.grad[i] *= scale
                        i++
                    }
                }
            }
        }
        return norm
    }
}
