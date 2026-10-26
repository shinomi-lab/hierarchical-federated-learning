package com.example.hfl_experiment.training.core

import kotlin.math.ln
import kotlin.math.max
import com.example.hfl_experiment.training.optim.AdamFlat

class LinearModel(
    val inputSize: Int,
    val numClasses: Int,
    var labelSmoothing: Float = 0f,
    var weightDecay: Float = 0f,        // L2 正則化（biasは除外）
    private val seed: Long? = null
) {
    val weights = Array(numClasses) { FloatArray(inputSize) }
    val biases  = FloatArray(numClasses) { 0f }
    private val adam = AdamFlat(lr = 1e-3f)

    init {
        val rnd = if (seed != null) kotlin.random.Random(seed) else kotlin.random.Random.Default
        val scale = 1f / kotlin.math.sqrt(inputSize.toFloat())
        for (c in 0 until numClasses) for (i in 0 until inputSize)
            weights[c][i] = (rnd.nextFloat() - 0.5f) * 2f * scale
    }

    // --- forward utils ---
    private fun logits(x: FloatArray): FloatArray {
        val z = FloatArray(numClasses)
        for (c in 0 until numClasses) {
            var s = biases[c]
            val wc = weights[c]
            for (i in x.indices) s += wc[i] * x[i]
            z[c] = s
        }
        return z
    }

    private fun softmax(z: FloatArray): FloatArray {
        var maxZ = z[0]; for (i in 1 until z.size) if (z[i] > maxZ) maxZ = z[i]
        var sum = 0.0
        val exps = DoubleArray(z.size)
        for (i in z.indices) { val e = kotlin.math.exp((z[i] - maxZ).toDouble()); exps[i] = e; sum += e }
        val p = FloatArray(z.size); val inv = 1.0 / sum
        for (i in z.indices) p[i] = (exps[i] * inv).toFloat()
        return p
    }

    private fun crossEntropy(p: FloatArray, y: Int, eps: Float = 1e-7f): Float {
        if (labelSmoothing <= 0f) return -ln(max(p[y], eps))
        val k = p.size; val epsEach = labelSmoothing / (k - 1)
        var loss = 0.0
        for (c in 0 until k) {
            val target = if (c == y) 1f - labelSmoothing else epsEach
            loss += (-target * ln(max(p[c], eps)))
        }
        return loss.toFloat()
    }

    // --- flatten helpers (weights -> flat, bias続き) ---
    fun flattenParams(): FloatArray {
        val out = FloatArray(numClasses * inputSize + numClasses)
        var k = 0
        for (c in 0 until numClasses) for (i in 0 until inputSize) out[k++] = weights[c][i]
        for (c in 0 until numClasses) out[k++] = biases[c]
        return out
    }

    fun loadParams(buf: FloatArray) {
        require(buf.size == numClasses * inputSize + numClasses)
        var k = 0
        for (c in 0 until numClasses) for (i in 0 until inputSize) weights[c][i] = buf[k++]
        for (c in 0 until numClasses) biases[c] = buf[k++]
    }

    // --- 1サンプル更新（Adam + L2 weight decay） ---
    fun stepAdam(x: FloatArray, label: Int): Float {
        val z = logits(x)
        val p = softmax(z)
        val loss = crossEntropy(p, label)

        // dL/dz = p - y_smooth
        val k = p.size
        val epsEach = if (labelSmoothing > 0f) labelSmoothing / (k - 1) else 0f
        val dLogits = FloatArray(k)
        for (c in 0 until k) {
            val y = if (labelSmoothing > 0f) {
                if (c == label) 1f - labelSmoothing else epsEach
            } else {
                if (c == label) 1f else 0f
            }
            dLogits[c] = p[c] - y
        }

        // 勾配をフラットに作る
        val grad = FloatArray(numClasses * inputSize + numClasses)
        var gk = 0
        for (c in 0 until numClasses) {
            val dl = dLogits[c]
            val wc = weights[c]
            for (i in 0 until inputSize) {
                // dL/dW = dl * x_i + weightDecay * W  (L2; biasは除外)
                val g = dl * x[i] + (if (weightDecay > 0f) weightDecay * wc[i] else 0f)
                grad[gk++] = g
            }
        }
        for (c in 0 until numClasses) {
            grad[gk++] = dLogits[c] // dL/db
        }

        // パラメータをフラットにして Adam 更新
        val param = flattenParams()
        adam.step(param, grad)
        loadParams(param)

        return loss
    }
}
