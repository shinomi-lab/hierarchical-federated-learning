package com.example.hfl_experiment.training.optim

import kotlin.math.sqrt
import kotlin.math.max

/**
 * フラット配列用の軽量 Adam (AdamW なし / L2は呼び出し側で実施)
 * - パラメータ配列 param と同サイズの grad を受けてその場更新
 * - AMSGrad オプションあり
 */
class AdamFlat(
    private val lr: Float = 1e-3f,
    private val betas: Pair<Float, Float> = 0.9f to 0.999f,
    private val eps: Float = 1e-8f,
    private val amsgrad: Boolean = false
) {
    private var stepCount = 0
    private var m: FloatArray? = null
    private var v: FloatArray? = null
    private var vHat: FloatArray? = null // AMSGrad 用 max(v)

    fun reset() {
        stepCount = 0
        m = null
        v = null
        vHat = null
    }

    fun step(param: FloatArray, grad: FloatArray) {
        require(param.size == grad.size) { "param and grad must have same length" }
        if (m == null || m!!.size != param.size) {
            m = FloatArray(param.size)
            v = FloatArray(param.size)
            vHat = if (amsgrad) FloatArray(param.size) else null
        }
        stepCount += 1
        val (beta1, beta2) = betas
        val mArr = m!!
        val vArr = v!!
        val vHatArr = vHat

        // バイアス補正係数
        val b1t = 1f - beta1.pow(stepCount)
        val b2t = 1f - beta2.pow(stepCount)

        // 1次・2次モーメント更新
        var i = 0
        while (i < param.size) {
            val g = grad[i]
            mArr[i] = beta1 * mArr[i] + (1f - beta1) * g
            vArr[i] = beta2 * vArr[i] + (1f - beta2) * g * g
            if (vHatArr != null) vHatArr[i] = max(vHatArr[i], vArr[i])
            i++
        }

        // バイアス補正して更新
        val denomArray = vHatArr ?: vArr
        i = 0
        while (i < param.size) {
            val mHat = mArr[i] / b1t
            val vHatCorr = denomArray[i] / b2t
            val denom = (sqrt(vHatCorr.toDouble()) + eps).toFloat()
            param[i] -= lr * (mHat / denom)
            i++
        }
    }

    // 軽量 float^int
    private fun Float.pow(e: Int): Float {
        var r = 1f
        repeat(e) { r *= this }
        return r
    }
}
