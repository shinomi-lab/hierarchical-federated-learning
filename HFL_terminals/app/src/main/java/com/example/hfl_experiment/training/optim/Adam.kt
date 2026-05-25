package com.example.hfl_experiment.training.optim

import java.util.IdentityHashMap
import kotlin.math.max
import kotlin.math.sqrt

/**
 * 高度設計の Adam:
 * - AdamW (decoupled weight decay)
 * - AMSGrad オプション
 * - 複数 ParamGroup
 */
class Adam(
    private val groups: List<ParamGroup> = emptyList()
) : Optimizer {

    /** state は ParamRef の参照IDをキーに保持（identityベース） */
    private data class State(
        val m: FloatArray,
        val v: FloatArray,
        val vHat: FloatArray?, // AMSGrad のための max(v)
    )

    // ❌ 重複定義を削除。MutableMap として宣言（getOrPut 使用のため）
    private val stateMap: MutableMap<ParamRef, State> = IdentityHashMap()

    private var stepCount: Int = 0

    override fun paramGroups(): List<ParamGroup> = groups

    override fun reset() {
        stateMap.clear()
        stepCount = 0
    }

    override fun step() {
        stepCount += 1
        for (g in groups) {
            val (beta1, beta2) = g.betas
            val lr  = g.lr
            val eps = g.eps
            val wd  = g.weightDecay
            val useAMSG = g.amsgrad

            // バイアス補正係数 (1 - beta^t)
            val b1t = 1f - beta1.pow(stepCount)
            val b2t = 1f - beta2.pow(stepCount)

            for (p in g.params) {
                val s = stateFor(p, useAMSG)
                val m = s.m
                val v = s.v
                val vHat = s.vHat

                val grad = p.grad
                //（任意）安全確認
                // require(grad.size == p.length)

                // 1次・2次モーメント更新
                var i = 0
                while (i < p.length) {
                    val g_i = grad[i]
                    m[i] = beta1 * m[i] + (1f - beta1) * g_i
                    v[i] = beta2 * v[i] + (1f - beta2) * g_i * g_i
                    if (useAMSG) {
                        vHat!![i] = max(vHat[i], v[i])
                    }
                    i++
                }

                // バイアス補正 + 更新
                val denomArray = if (useAMSG) vHat!! else v
                i = 0
                while (i < p.length) {
                    val mHat = m[i] / b1t
                    val vHatCorr = denomArray[i] / b2t

                    // sqrt は Double を返すので Float に揃える
                    val denom = (sqrt(vHatCorr.toDouble()) + eps).toFloat()

                    // AdamW: decoupled weight decay（パラメータ自体を減衰）
                    if (wd != 0f) {
                        val idx = p.offset + i
                        p.array[idx] -= lr * wd * p.array[idx]
                    }

                    // パラメータ更新
                    p.array[p.offset + i] -= lr * (mHat / denom)
                    i++
                }
            }
        }
    }

    private fun stateFor(p: ParamRef, amsgrad: Boolean): State {
        return stateMap.getOrPut(p) {
            val m = FloatArray(p.length)
            val v = FloatArray(p.length)
            val vHat = if (amsgrad) FloatArray(p.length) else null
            State(m, v, vHat)
        }
    }

    // Float pow（高速で十分）
    private fun Float.pow(e: Int): Float {
        var r = 1f
        repeat(e) { r *= this }
        return r
    }
}
