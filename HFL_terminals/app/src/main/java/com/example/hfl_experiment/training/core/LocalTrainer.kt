package com.example.hfl_experiment.training.core

import com.example.hfl_experiment.training.data.DataSample
import kotlin.math.exp
import kotlin.math.ln
import kotlin.math.max
import kotlin.math.pow
import kotlin.math.sqrt
import kotlin.random.Random
import com.google.gson.annotations.SerializedName
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.yield
import kotlin.coroutines.coroutineContext
import kotlinx.coroutines.Job
import kotlinx.coroutines.ensureActive
import com.example.hfl_experiment.util.logging.RealTimeLogger
import java.nio.ByteBuffer
import java.nio.ByteOrder

// =================================================================================
// データ関連クラス (同じパッケージ内の別ファイルでも可)
// =================================================================================

data class ModelParameters(
    val weights1: List<List<Float>>,
    val biases1: List<Float>,
    val norm1Gamma: List<Float>,
    val norm1Beta: List<Float>,
    val weights2: List<List<Float>>,
    val biases2: List<Float>,
    val norm2Gamma: List<Float>,
    val norm2Beta: List<Float>,
    val weights3: List<List<Float>>,
    val biases3: List<Float>
    // 仮想ルータID: 送信時に現在の論理ルータを含めるためのフィールド
    , @SerializedName("virtual_router_id") val virtualRouterId: String = "HFL_A24"
)

/**
 * データセットの一部を表すクラス
 * Pythonの DatasetSplit に相当
 */



// =================================================================================
// メインクラス: LocalTrainer
// =================================================================================

class LocalTrainer(
    private val inputSize: Int,
    private val hiddenSize: Int,
    private val outputSize: Int,
    private val dropoutP: Float,
    private val learningRate: Float,
    private val weightDecay: Float,
    private val localEpochs: Int,
    private val localBatchSize: Int,
    private val clipMaxNorm: Float,
    private val warmupSteps: Int,
    private val totalSteps: Int,
    private val minLrRatio: Float,
    private val logger: (String) -> Unit,

    // fullDataset と userIndices をコンストラクタで受け取るように修正
    private val fullDataset: List<DataSample>,
    private val userIndices: List<Int>,
    // optional: caller can supply application filesDir to ensure logs are written to correct app storage
    private val appFilesDir: File? = null,
    // Memory optimization: when false, disables buffer reuse (for A/B testing)
    private val memoryOptEnabled: Boolean = true,
) {
    // --- 内部クラス: 各レイヤーとモデル ---
    private data class LayerOutput(val output: List<FloatArray>, val intermediates: Map<String, Any> = emptyMap())

    private inner class LinearLayer(val inputDim: Int, val outputDim: Int) {
        var weights = MutableList(outputDim) { MutableList(inputDim) { Random.nextFloat() * 2f / sqrt(inputDim.toFloat()) - 1f / sqrt(inputDim.toFloat()) } }
        var biases = MutableList(outputDim) { 0.0f }

        // Reusable output buffer to avoid per-batch allocation
        private var outputBuf: Array<FloatArray>? = null

        fun forward(inputs: List<FloatArray>): LayerOutput {
            val batchSize = inputs.size
            val buf = if (memoryOptEnabled) {
                outputBuf?.takeIf { it.size == batchSize && (it.isNotEmpty() && it[0].size == outputDim) }
                    ?: Array(batchSize) { FloatArray(outputDim) }.also { outputBuf = it }
            } else {
                Array(batchSize) { FloatArray(outputDim) }
            }
            for (b in 0 until batchSize) {
                for (j in 0 until outputDim) {
                    var sum = 0.0f
                    for (i in 0 until inputDim) {
                        sum += inputs[b][i] * weights[j][i]
                    }
                    buf[b][j] = sum + biases[j]
                }
            }
            return LayerOutput(buf.toList(), mapOf("inputs" to inputs))
        }

        // 修正: インデックスの範囲チェックを追加してエラーを防止
        fun backward(dOutputs: List<FloatArray>, intermediates: Map<String, Any>): Pair<List<FloatArray>, Gradients> {
            val inputs = intermediates["inputs"] as List<FloatArray>
            val batchSize = inputs.size
            val dInputs = MutableList(batchSize) { FloatArray(inputDim) }
            val dWeights = MutableList(outputDim) { MutableList(inputDim) { 0.0f } }
            val dBiases = MutableList(outputDim) { 0.0f } // 初期化を確認

            for (b in 0 until batchSize) {
                for (j in 0 until outputDim) {
                    val dOut = dOutputs[b][j]
                    if (j < dBiases.size) { // 範囲チェックを追加
                        dBiases[j] = dBiases[j] + dOut
                    }
                    for (i in 0 until inputDim) {
                        if (j < dWeights.size && i < dWeights[j].size) { // 範囲チェックを追加
                            dWeights[j][i] = dWeights[j][i] + dOut * inputs[b][i]
                        }
                        if (i < dInputs[b].size) { // 範囲チェックを追加
                            dInputs[b][i] = dInputs[b][i] + dOut * weights[j][i]
                        }
                    }
                }
            }
            // 平均勾配の計算
            if (batchSize > 0) {
                for (j in 0 until outputDim) {
                    if (j < dBiases.size) { // 範囲チェックを追加
                        dBiases[j] = dBiases[j] / batchSize
                    }
                    for (i in 0 until inputDim) {
                        if (j < dWeights.size && i < dWeights[j].size) { // 範囲チェックを追加
                            dWeights[j][i] = dWeights[j][i] / batchSize
                        }
                    }
                }
            }
            return Pair(dInputs, Gradients(dWeights, dBiases))
        }
    }

    private inner class LayerNorm(val dim: Int, val epsilon: Float = 1e-5f) {
        var gamma = MutableList(dim) { 1.0f } // scale
        var beta  = MutableList(dim) { 0.0f } // shift

        // Reusable buffers
        private var outputBuf: Array<FloatArray>? = null
        private var normBuf: Array<FloatArray>? = null
        private var meansBuf: FloatArray? = null
        private var variancesBuf: FloatArray? = null

        fun forward(inputs: List<FloatArray>): LayerOutput {
            val batchSize = inputs.size
            val outputs = if (memoryOptEnabled) {
                outputBuf?.takeIf { it.size == batchSize }
                    ?: Array(batchSize) { FloatArray(dim) }.also { outputBuf = it }
            } else { Array(batchSize) { FloatArray(dim) } }
            val inputsNormalized = if (memoryOptEnabled) {
                normBuf?.takeIf { it.size == batchSize }
                    ?: Array(batchSize) { FloatArray(dim) }.also { normBuf = it }
            } else { Array(batchSize) { FloatArray(dim) } }
            val means = if (memoryOptEnabled) {
                meansBuf?.takeIf { it.size == batchSize }
                    ?: FloatArray(batchSize).also { meansBuf = it }
            } else { FloatArray(batchSize) }
            val variances = if (memoryOptEnabled) {
                variancesBuf?.takeIf { it.size == batchSize }
                    ?: FloatArray(batchSize).also { variancesBuf = it }
            } else { FloatArray(batchSize) }

            for (b in 0 until batchSize) {
                var sum = 0f
                for (i in 0 until dim) sum += inputs[b][i]
                val mean = sum / dim
                means[b] = mean
                var varSum = 0f
                for (i in 0 until dim) { val d = inputs[b][i] - mean; varSum += d * d }
                val variance = varSum / dim
                variances[b] = variance
                val stdInv = 1f / sqrt(variance + epsilon)
                for (i in 0 until dim) {
                    val xn = (inputs[b][i] - mean) * stdInv
                    inputsNormalized[b][i] = xn
                    outputs[b][i] = xn * gamma[i] + beta[i]
                }
            }
            return LayerOutput(
                outputs.toList(),
                mapOf(
                    "inputs" to inputs,
                    "inv_std" to means.indices.map { 1f / sqrt(variances[it] + epsilon) },
                    "x_norm" to inputsNormalized.toList()
                )
            )
        }

        fun backward(
            dOutputs: List<FloatArray>,
            intermediates: Map<String, Any>
        ): Triple<List<FloatArray>, Gradients, Gradients> {

            val inputs = intermediates["inputs"] as List<FloatArray>
            val invStd = intermediates["inv_std"] as List<Float>
            // ★ ここが修正点：保存した型に合わせて FloatArray のリストで受ける
            val xNorm  = intermediates["x_norm"]  as List<FloatArray>

            val batchSize = inputs.size
            val dBeta  = MutableList(dim) { 0f }
            val dGamma = MutableList(dim) { 0f }
            val dInputs = MutableList(batchSize) { FloatArray(dim) }

            for (b in 0 until batchSize) {
                val mean = inputs[b].average().toFloat()
                val dxNorm = FloatArray(dim)
                // γ, β の勾配 & dxNorm
                for (i in 0 until dim) {
                    val dyi = dOutputs[b][i]
                    dxNorm[i] = dyi * gamma[i]
                    dGamma[i] += dyi * xNorm[b][i]
                    dBeta[i]  += dyi
                }

                // var, mean の勾配
                val inv = invStd[b]
                val inv3 = inv.toDouble().pow(3.0) // (invStd)^3 は Double で計算
                val dVar = dxNorm.zip(xNorm[b]).sumOf { (dxn, xn) -> (dxn * xn).toDouble() } * -0.5 * inv3
                val sumDx = dxNorm.sum()
                val diffSum = inputs[b].sumOf { (it - mean).toDouble() }

                val dMean = sumDx * -inv +
                        dVar * (-2.0 * diffSum / dim.toDouble())

                for (i in 0 until dim) {
                    val diff = inputs[b][i] - mean
                    dInputs[b][i] =
                        dxNorm[i] * inv +
                                (dVar * 2.0 * diff / dim.toDouble()).toFloat() +
                                (dMean / dim.toDouble()).toFloat()
                }
            }

            // 平均化
            if (batchSize > 0) {
                for (i in 0 until dim) {
                    dBeta[i] = dBeta[i] / batchSize
                    dGamma[i] = dGamma[i] / batchSize
                }
            }
            // LayerNorm は重み（gamma/beta）のみ。dWeights は空でOK
            return Triple(
                dInputs,
                Gradients(mutableListOf(), dBeta),
                Gradients(mutableListOf(), dGamma)
            )
        }
    }


    private inner class ReLU {
        private var outputBuf: Array<FloatArray>? = null

        fun forward(inputs: List<FloatArray>): LayerOutput {
            val batchSize = inputs.size
            val dim = if (batchSize > 0) inputs[0].size else 0
            val buf = if (memoryOptEnabled) {
                outputBuf?.takeIf { it.size == batchSize && (batchSize == 0 || it[0].size == dim) }
                    ?: Array(batchSize) { FloatArray(dim) }.also { outputBuf = it }
            } else {
                Array(batchSize) { FloatArray(dim) }
            }
            for (b in 0 until batchSize) {
                val inp = inputs[b]
                for (i in inp.indices) buf[b][i] = max(0f, inp[i])
            }
            return LayerOutput(buf.toList(), mapOf("inputs" to inputs))
        }
        fun backward(dOutputs: List<FloatArray>, intermediates: Map<String, Any>): List<FloatArray> {
            val inputs = intermediates["inputs"] as List<FloatArray>
            return dOutputs.zip(inputs).map { (d, inp) ->
                FloatArray(d.size) { i -> if (inp[i] > 0) d[i] else 0f }
            }
        }
    }

    private inner class Dropout(private val p: Float) {
        private var masks: List<FloatArray>? = null
        private var isTraining = true
        fun trainMode() { isTraining = true }
        fun evalMode() { isTraining = false }

        fun forward(inputs: List<FloatArray>): LayerOutput {
            if (!isTraining || p == 0f) return LayerOutput(inputs)
            masks = inputs.map { row ->
                FloatArray(row.size) { if (Random.nextFloat() > p) 1f / (1f - p) else 0f }
            }
            val outputs = inputs.zip(masks!!).map { (row, mask) ->
                row.zip(mask).map { (x, m) -> x * m }.toFloatArray()
            }
            return LayerOutput(outputs)
        }
        fun backward(dOutputs: List<FloatArray>, intermediates: Map<String, Any>): List<FloatArray> {
            if (!isTraining || p == 0f || masks == null) return dOutputs
            return dOutputs.zip(masks!!).map { (d, mask) ->
                d.zip(mask).map { (d_val, m_val) -> d_val * m_val }.toFloatArray()
            }
        }
    }

    private inner class MLPModel {
        private val layer1 = LinearLayer(inputSize, hiddenSize)
        private val norm1 = LayerNorm(hiddenSize)
        private val relu1 = ReLU()
        private val dropout1 = Dropout(dropoutP)

        private val layer2 = LinearLayer(hiddenSize, hiddenSize)
        private val norm2 = LayerNorm(hiddenSize)
        private val relu2 = ReLU()
        private val dropout2 = Dropout(dropoutP)

        private val layer3 = LinearLayer(hiddenSize, outputSize)
        
        // ★追加: トレーニングモードフラグ
        private var isTraining = true

        fun trainMode() {
            isTraining = true
            dropout1.trainMode()
            dropout2.trainMode()
        }
        fun evalMode() {
            isTraining = false
            dropout1.evalMode()
            dropout2.evalMode()
        }

        private var forwardCache: MutableMap<String, Any> = mutableMapOf()

        fun forward(inputs: List<FloatArray>): List<FloatArray> {
            // ★修正: 推論時(evalMode)はキャッシュをクリア・保存しないことでGCを減らす
            if (isTraining) {
                forwardCache.clear()
                val l1_out = layer1.forward(inputs).also { forwardCache["l1"] = it.intermediates }
                val n1_out = norm1.forward(l1_out.output).also { forwardCache["n1"] = it.intermediates }
                val r1_out = relu1.forward(n1_out.output).also { forwardCache["r1"] = it.intermediates }
                val d1_out = dropout1.forward(r1_out.output)
                val l2_out = layer2.forward(d1_out.output).also { forwardCache["l2"] = it.intermediates }
                val n2_out = norm2.forward(l2_out.output).also { forwardCache["n2"] = it.intermediates }
                val r2_out = relu2.forward(n2_out.output).also { forwardCache["r2"] = it.intermediates }
                val d2_out = dropout2.forward(r2_out.output)
                val l3_out = layer3.forward(d2_out.output).also { forwardCache["l3"] = it.intermediates }
                return l3_out.output
            } else {
                // 推論モード: 中間データを保存しない
                val l1_out = layer1.forward(inputs)
                val n1_out = norm1.forward(l1_out.output)
                val r1_out = relu1.forward(n1_out.output)
                val d1_out = dropout1.forward(r1_out.output)
                val l2_out = layer2.forward(d1_out.output)
                val n2_out = norm2.forward(l2_out.output)
                val r2_out = relu2.forward(n2_out.output)
                val d2_out = dropout2.forward(r2_out.output)
                val l3_out = layer3.forward(d2_out.output)
                return l3_out.output
            }
        }

        fun backward(dLogits: List<FloatArray>): Map<String, Gradients> {
            val (dL3, grad3) = layer3.backward(dLogits, forwardCache["l3"] as Map<String, Any>)
            val dD2 = dropout2.backward(dL3, emptyMap())
            val dR2 = relu2.backward(dD2, forwardCache["r2"] as Map<String, Any>)
            val (dN2, grad_n2_b, grad_n2_g) = norm2.backward(dR2, forwardCache["n2"] as Map<String, Any>)
            val (dL2, grad2) = layer2.backward(dN2, forwardCache["l2"] as Map<String, Any>)
            val dD1 = dropout1.backward(dL2, emptyMap())
            val dR1 = relu1.backward(dD1, forwardCache["r1"] as Map<String, Any>)
            val (dN1, grad_n1_b, grad_n1_g) = norm1.backward(dR1, forwardCache["n1"] as Map<String, Any>)
            val (_, grad1) = layer1.backward(dN1, forwardCache["l1"] as Map<String, Any>)

            // Release intermediate references immediately after backward pass
            if (memoryOptEnabled) forwardCache.clear()

            return mapOf(
                "l1" to grad1, "n1_b" to grad_n1_b, "n1_g" to grad_n1_g,
                "l2" to grad2, "n2_b" to grad_n2_b, "n2_g" to grad_n2_g,
                "l3" to grad3
            )
        }

        fun getParameters(): List<ParamRef> = listOf(
            ParamRef(layer1.weights, layer1.biases),
            ParamRef(mutableListOf(norm1.gamma), norm1.beta, isNorm = true),
            ParamRef(layer2.weights, layer2.biases),
            ParamRef(mutableListOf(norm2.gamma), norm2.beta, isNorm = true),
            ParamRef(layer3.weights, layer3.biases)
        )

        fun getModelParameters(): ModelParameters = ModelParameters(
            weights1 = layer1.weights.map { it.toList() }, biases1 = layer1.biases.toList(),
            norm1Gamma = norm1.gamma.toList(), norm1Beta = norm1.beta.toList(),
            weights2 = layer2.weights.map { it.toList() }, biases2 = layer2.biases.toList(),
            norm2Gamma = norm2.gamma.toList(), norm2Beta = norm2.beta.toList(),
            weights3 = layer3.weights.map { it.toList() }, biases3 = layer3.biases.toList()
            // virtualRouterId はここではデフォルト値を使う（呼び出し側で上書き可）
            , virtualRouterId = "HFL_A24"
        )

        // --- 新規: ModelParameters から現在のモデルに値をセットする ---
        fun loadFromModelParameters(mp: ModelParameters) {
            // shallow checks (sizes should match expected dims)
            if (mp.weights1.size != hiddenSize || mp.weights1.any { it.size != inputSize }) {
                throw IllegalArgumentException("weights1 shape mismatch: expected ${hiddenSize}x${inputSize}, got ${mp.weights1.size}x${if (mp.weights1.isNotEmpty()) mp.weights1[0].size else 0}")
            }
            if (mp.weights2.size != hiddenSize || mp.weights2.any { it.size != hiddenSize }) {
                throw IllegalArgumentException("weights2 shape mismatch: expected ${hiddenSize}x${hiddenSize}")
            }
            if (mp.weights3.size != outputSize || mp.weights3.any { it.size != hiddenSize }) {
                throw IllegalArgumentException("weights3 shape mismatch: expected ${outputSize}x${hiddenSize}")
            }

            // assign
            layer1.weights = mp.weights1.map { it.toMutableList() }.toMutableList()
            layer1.biases = mp.biases1.toMutableList()
            norm1.gamma = mp.norm1Gamma.toMutableList()
            norm1.beta = mp.norm1Beta.toMutableList()

            layer2.weights = mp.weights2.map { it.toMutableList() }.toMutableList()
            layer2.biases = mp.biases2.toMutableList()
            norm2.gamma = mp.norm2Gamma.toMutableList()
            norm2.beta = mp.norm2Beta.toMutableList()

            layer3.weights = mp.weights3.map { it.toMutableList() }.toMutableList()
            layer3.biases = mp.biases3.toMutableList()
        }

        // --- 新規: flat 配列から読み込む（TrainingManager と同じ flatten order を期待） ---
        fun loadFromFlat(flat: FloatArray, inputSizeArg: Int, hiddenSizeArg: Int, outputSizeArg: Int) {
            // validate dims match
            if (inputSizeArg != inputSize || hiddenSizeArg != hiddenSize || outputSizeArg != outputSize) {
                throw IllegalArgumentException("Provided dims don't match this trainer's dims")
            }

            var idx = 0
            fun take(n: Int): List<Float> {
                if (idx + n > flat.size) throw IllegalArgumentException("flat array too short: need $n at idx $idx")
                val r = flat.slice(idx until idx + n)
                idx += n
                return r
            }

            val w1 = List(hiddenSize) { r ->
                take(inputSize).toMutableList()
            }
            val b1 = take(hiddenSize)
            val g1 = take(hiddenSize)
            val be1 = take(hiddenSize)
            val w2 = List(hiddenSize) { r ->
                take(hiddenSize).toMutableList()
            }
            val b2 = take(hiddenSize)
            val g2 = take(hiddenSize)
            val be2 = take(hiddenSize)
            val w3 = List(outputSize) { r ->
                take(hiddenSize).toMutableList()
            }
            val b3 = take(outputSize)

            val mp = ModelParameters(
                weights1 = w1,
                biases1 = b1,
                norm1Gamma = g1,
                norm1Beta = be1,
                weights2 = w2,
                biases2 = b2,
                norm2Gamma = g2,
                norm2Beta = be2,
                weights3 = w3,
                biases3 = b3
            )
            loadFromModelParameters(mp)
        }
    }

    // expose loader methods to outer class
    fun loadModelParameters(mp: ModelParameters) { val startTime = System.currentTimeMillis()
        RealTimeLogger.d("ModelLoad", "Loading model parameters started at: $startTime")

        model.loadFromModelParameters(mp)

        val endTime = System.currentTimeMillis()
        RealTimeLogger.d("ModelLoad", "Loading model parameters finished at: $endTime")
        RealTimeLogger.d("ModelLoad", "Total time taken to load model parameters: ${endTime - startTime} ms")
    }

    fun loadFromFlat(flat: FloatArray, inputSizeArg: Int, hiddenSizeArg: Int, outputSizeArg: Int) {
        model.loadFromFlat(flat, inputSizeArg, hiddenSizeArg, outputSizeArg)
    }

    // --- Add missing helper data classes used by optimizer and model wiring ---
    private data class Gradients(val dWeights: MutableList<MutableList<Float>>, val dBiases: MutableList<Float>)
    private data class ParamRef(val weights: MutableList<MutableList<Float>>, val biases: MutableList<Float>, val isNorm: Boolean = false)

    // --- Optimizer と Scheduler ---
    private inner class AdamW(
        private val paramRefs: List<ParamRef>,
        private var lr: Float,
        private val weightDecay: Float,
        private val beta1: Float = 0.9f,
        private val beta2: Float = 0.999f,
        private val epsilon: Float = 1e-8f
    ) {
        // 修正箇所： .map の後に .toMutableList() を追加
        private val m = paramRefs.map {
            Gradients(
                it.weights.map { r -> MutableList(r.size) { 0f } }.toMutableList(), // dWeightsをMutableListに
                MutableList(it.biases.size) { 0f }
            )
        }.toMutableList() // 外側のリストもMutableListに

        private val v = paramRefs.map {
            Gradients(
                it.weights.map { r -> MutableList(r.size) { 0f } }.toMutableList(), // dWeightsをMutableListに
                MutableList(it.biases.size) { 0f }
            )
        }.toMutableList() // 外側のリストもMutableListに

        private var t: Int = 0

        fun setLearningRate(newLr: Float) { lr = newLr }

        fun step(grads: Map<String, Gradients>, clipMaxNorm: Float) {
            t++
            val gradList = listOfNotNull(grads["l1"], grads["n1_g"], grads["n1_b"], grads["l2"], grads["n2_g"], grads["n2_b"], grads["l3"])

            // 勾配クリッピング
            var totalNorm = 0.0
            gradList.forEach { grad ->
                grad.dWeights.forEach { r -> r.forEach { g -> totalNorm += g * g } }
                grad.dBiases.forEach { g -> totalNorm += g * g }
            }
            totalNorm = sqrt(totalNorm)
            val clipCoef = if (totalNorm > clipMaxNorm && totalNorm > 0) (clipMaxNorm / totalNorm.toFloat()) else 1.0f

            // 更新
            paramRefs.forEachIndexed { i, ref ->
                if(ref.isNorm) { // LayerNorm
                    val gammaGrads = grads.getValue("n${i/2 + 1}_g")
                    val betaGrads = grads.getValue("n${i/2 + 1}_b")
                    updateParam(ref.weights[0], gammaGrads.dBiases, m[i].dWeights[0], v[i].dWeights[0], clipCoef, isNormParam=true)
                    updateParam(ref.biases, betaGrads.dBiases, m[i].dBiases, v[i].dBiases, clipCoef, isNormParam=true)
                } else { // Linear
                    val gradKey = when(i) {
                        0 -> "l1"
                        2 -> "l2"
                        4 -> "l3"
                        else -> ""
                    }
                    if (gradKey.isNotEmpty()) {
                        val currentGrad = grads.getValue(gradKey)
                        updateParam(ref.weights, currentGrad.dWeights, m[i].dWeights, v[i].dWeights, clipCoef)
                        updateParam(ref.biases, currentGrad.dBiases, m[i].dBiases, v[i].dBiases, clipCoef)
                    }
                }
            }
        }

        @JvmName("updateParamNested") // 修正: JVM上での名前衝突を避ける
        private fun updateParam(params: MutableList<MutableList<Float>>, grads: List<List<Float>>, m_state: MutableList<MutableList<Float>>, v_state: MutableList<MutableList<Float>>, clip: Float, isNormParam:Boolean=false) {
            for(r in params.indices) for(c in params[r].indices) {
                val g = grads[r][c] * clip
                m_state[r][c] = beta1 * m_state[r][c] + (1 - beta1) * g
                v_state[r][c] = beta2 * v_state[r][c] + (1 - beta2) * g * g
                val m_hat = m_state[r][c] / (1 - beta1.pow(t))
                val v_hat = v_state[r][c] / (1 - beta2.pow(t))
                val wd = if(isNormParam) 0f else weightDecay * params[r][c]
                params[r][c] -= lr * (m_hat / (sqrt(v_hat) + epsilon) + wd)
            }
        }

        private fun updateParam(params: MutableList<Float>, grads: List<Float>, m_state: MutableList<Float>, v_state: MutableList<Float>, clip: Float, isNormParam:Boolean=false) {
            for(i in params.indices) {
                val g = grads[i] * clip
                m_state[i] = beta1 * m_state[i] + (1 - beta1) * g
                v_state[i] = beta2 * v_state[i] + (1 - beta2) * g * g
                val m_hat = m_state[i] / (1 - beta1.pow(t))
                val v_hat = v_state[i] / (1 - beta2.pow(t))
                val wd = if(isNormParam) 0f else weightDecay * params[i]
                params[i] -= lr * (m_hat / (sqrt(v_hat) + epsilon) + wd)
            }
        }
    }

    private inner class LinearWarmupScheduler(
        private val optimizer: AdamW, private val baseLr: Float,
        private val warmupSteps: Int, private val totalSteps: Int, private val minLrRatio: Float
    ) {
        private var currentStep = 0
        fun step() {
            currentStep++
            val newLr = when {
                currentStep < warmupSteps -> baseLr * (currentStep.toFloat() / warmupSteps.toFloat())
                currentStep > totalSteps -> baseLr * minLrRatio
                else -> {
                    val decayRatio = (currentStep - warmupSteps).toFloat() / (totalSteps - warmupSteps).toFloat()
                    baseLr * ((1 - minLrRatio) * (1 - decayRatio) + minLrRatio)
                }
            }
            optimizer.setLearningRate(newLr)
        }
    }

    private object CrossEntropyLoss {
        fun compute(logits: List<FloatArray>, targets: List<Int>): Pair<Float, List<FloatArray>> {
            val batchSize = logits.size
            if (batchSize == 0) return Pair(0f, emptyList())
            val dLogits = MutableList(batchSize) { FloatArray(logits[0].size) }
            var totalLoss = 0.0f
            for (b in 0 until batchSize) {
                val logitRow = logits[b]
                val target = targets[b]
                if (target !in logitRow.indices) continue // 安全対策
                val maxLogit = logitRow.maxOrNull() ?: 0f
                val exps = logitRow.map { exp(it - maxLogit) }
                val sumExps = exps.sum()
                val logSumExps = ln(sumExps)
                totalLoss += (maxLogit + logSumExps - logitRow[target])
                for (j in 0 until logitRow.size) {
                    val prob = exps[j] / sumExps
                    dLogits[b][j] = if (j == target) prob - 1f else prob
                }
            }
            return Pair(totalLoss / batchSize, dLogits)
        }
    }

    // --- メインクラスのプロパティとメソッド ---

    private val model: MLPModel
    private val optimizer: AdamW
    private val scheduler: LinearWarmupScheduler
    private val trainLoaderBatches: List<List<DataSample>>
    private val validLoaderBatches: List<List<DataSample>>
    private val testLoaderBatches: List<List<DataSample>>

    private val logFile: File

    init {
        RealTimeLogger.d("LocalTrainer", "初期化開始...")
        model = MLPModel()
        val params = model.getParameters()
        optimizer = AdamW(params, learningRate, weightDecay)
        scheduler = LinearWarmupScheduler(optimizer, learningRate, warmupSteps, totalSteps, minLrRatio)

        val n = userIndices.size
        // Robust Train/Val/Test split: default 80% / 10% / 10% with at-least-1 guarding
        var trainCount = (n * 0.8f).toInt()
        var valCount = (n * 0.1f).toInt()
        var testCount = n - trainCount - valCount
        if (n > 0 && trainCount < 1) trainCount = 1
        if (testCount <= 0 && n - trainCount > 0) {
            testCount = 1
            valCount = n - trainCount - testCount
        }
        if (valCount <= 0 && n - trainCount - testCount > 0) {
            valCount = 1
            trainCount = n - valCount - testCount
        }
        if (trainCount + valCount + testCount != n) {
            // adjust train to consume remainder
            trainCount = n - valCount - testCount
            if (trainCount < 1) trainCount = 1
        }
        val idxsTrain = userIndices.subList(0, trainCount.coerceAtMost(n))
        val idxsVal = if (trainCount < n) userIndices.subList(trainCount, (trainCount + valCount).coerceAtMost(n)) else emptyList()
        val idxsTest = if (trainCount + valCount < n) userIndices.subList((trainCount + valCount).coerceAtMost(n), n) else emptyList()

        RealTimeLogger.i("LocalTrainer", "Dataset split counts: total=$n train=${idxsTrain.size} val=${idxsVal.size} test=${idxsTest.size}")

        trainLoaderBatches = DatasetSplit(fullDataset, idxsTrain).generateBatches(localBatchSize, shuffle = true)
        validLoaderBatches = if (idxsVal.isNotEmpty()) DatasetSplit(fullDataset, idxsVal).generateBatches(localBatchSize, shuffle = false) else emptyList()
        testLoaderBatches = if (idxsTest.isNotEmpty()) DatasetSplit(fullDataset, idxsTest).generateBatches(localBatchSize, shuffle = false) else emptyList()
        RealTimeLogger.i("LocalTrainer", "Batches counts: trainBatches=${trainLoaderBatches.size} valBatches=${validLoaderBatches.size} testBatches=${testLoaderBatches.size}")

        val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss").format(Date())
        val logFileName = "${timestamp}_log.txt"
        val filesDir = appFilesDir ?: File("data/data/com.example.hfl_experiment/files")
        val logDir = File(filesDir, "log")
        if (!logDir.exists()) logDir.mkdirs()
        logFile = File(logDir, logFileName)
        try { logFile.createNewFile() } catch (_: Throwable) {}

        log("ローカル学習者初期化完了")
        attemptLoadDownloadedModel() // 自動検出を初期化時に実行
    }

    private fun logToConsole(message: String) {
        logger.invoke(message)
        RealTimeLogger.d("LocalTrainer", message)
    }

    private fun logToFile(message: String) {
        try {
            val ts = SimpleDateFormat("yyyy-MM-dd HH:mm:ss,SSS", Locale.US).format(Date())
            val line = "$ts - INFO - LocalTrainer - $message\n"
            logFile.appendText(line)
        } catch (_: Throwable) { }
    }

    private fun log(message: String) {
        logToConsole(message)
        logToFile(message)
    }

    // 修正: 型キャストの警告を解消
    private fun safeCastIntermediates(key: String, intermediates: Map<String, Any>): List<FloatArray>? {
        return intermediates[key] as? List<FloatArray>
    }

    private fun safeCastFloatList(key: String, intermediates: Map<String, Any>): List<Float>? {
        return intermediates[key] as? List<Float>
    }

    // 修正: 配列アクセスエラーを解消
    private fun validateIndex(index: Int, size: Int): Boolean {
        return index in 0 until size
    }

    // 修正: SimpleDateFormatの警告を解消
    private fun getFormattedDate(): String {
        val locale = Locale.getDefault()
        val formatter = SimpleDateFormat("yyyyMMdd_HHmmss", locale)
        return formatter.format(Date())
    }

    private fun processBatch(batch: List<DataSample>?): Pair<List<FloatArray>, List<Int>>? {
        if (batch == null || batch.isEmpty()) return null
        val inputs = batch.map { it.features }
        val targets = batch.map { it.label }
        return Pair(inputs, targets)
    }

    suspend fun updateWeightsAndGetLoss(
        onEpochEnd: (epoch: Int, averageLoss: Float, averageAccuracy: Float) -> Unit,
        onProgress: (progressFraction: Float) -> Unit = {}
    ): Float {
        // ★ Ensure training uses trainLoaderBatches and validation uses validLoaderBatches
        val backupParams = getModelParameters()
        var nanOccurrences = 0

        model.trainMode()
        val epochLosses = mutableListOf<Float>()
        log("ローカル学習開始 (エポック数: $localEpochs)")

        val totalTrainSamples = trainLoaderBatches.sumOf { it.size }
        val totalTrainSamplesSafe = if (totalTrainSamples > 0) totalTrainSamples else 1

        for (epoch in 1..localEpochs) {
            coroutineContext.ensureActive()
            RealTimeLogger.i("LocalTrainer", "epoch_start: epoch=$epoch")
            var epochLossAcc = 0f
            var epochCorrect = 0
            var epochSamples = 0

            // Training loop over batches
            var batchOrdinal = 0
            for (batch in trainLoaderBatches) {
                if (batchOrdinal++ % 8 == 0) coroutineContext.ensureActive()
                val proc = processBatch(batch) ?: continue
                val (inputs, targets) = proc
                // forward
                val logits = model.forward(inputs)
                // compute loss and gradients
                val (loss, dlogits) = CrossEntropyLoss.compute(logits, targets)
                epochLossAcc += loss * targets.size
                epochSamples += targets.size
                // backward and optimization
                val grads = model.backward(dlogits)
                try {
                    optimizer.step(grads, clipMaxNorm)
                } catch (e: Exception) {
                    RealTimeLogger.w("LocalTrainer", "Optimizer step failed: ${e.message}")
                }
                scheduler.step()
            }

            // Validation at epoch end
            val (valLoss, valAcc) = inferenceOnBatches(validLoaderBatches)
            model.trainMode()   // restore training mode after validation
            val avgLossForEpoch = if (epochSamples > 0) epochLossAcc / epochSamples else 0f
            epochLosses.add(avgLossForEpoch)

            RealTimeLogger.i("LocalTrainer", "validation_end: epoch=$epoch valLoss=${"%.4f".format(valLoss)} valAcc=${"%.2f".format(valAcc * 100)}")
            onEpochEnd(epoch, valLoss, valAcc)
            try { onProgress((epoch.toFloat() / localEpochs.toFloat()).coerceIn(0f, 1f)) } catch (_: Exception) {}

            log("Epoch $epoch/$localEpochs: Train Loss = ${"%.4f".format(avgLossForEpoch)}, Val Loss = ${"%.4f".format(valLoss)}, Val Acc = ${"%.2f".format(valAcc * 100)}%")
        }

        log("ローカル学習完了。")
        val (testLoss, testAcc) = inferenceOnBatches(testLoaderBatches)
        try {
            val finalMsg = String.format(java.util.Locale.US, "Final Test: Loss = %.4f, Acc = %.2f%%", testLoss, testAcc * 100)
            log(finalMsg)
            RealTimeLogger.i("LocalTrainer", "final_test: loss=${"%.4f".format(testLoss)} acc=${"%.2f".format(testAcc * 100)}")
        } catch (e: Exception) {
            log("Final Test: Loss = $testLoss, Acc = ${testAcc * 100}%")
        }
        return epochLosses.average().toFloat()
    }

    // New helper to run inference over list of batches and return aggregated loss/acc
    fun inferenceOnBatches(batches: List<List<DataSample>>?): Pair<Float, Float> {
        if (batches.isNullOrEmpty()) return Pair(0f, 0f)
        model.evalMode()
        var totalLoss = 0f
        var correct = 0
        var total = 0
        for (batch in batches) {
            val proc = processBatch(batch) ?: continue
            val (inputs, targets) = proc
            val logits = model.forward(inputs)
            val (loss, _) = CrossEntropyLoss.compute(logits, targets)
            totalLoss += loss * targets.size
            // compute argmax and compare
            for (i in logits.indices) {
                val predIdx = logits[i].indices.maxByOrNull { logits[i][it] } ?: 0
                val target = targets[i]
                if (target in 0 until outputSize && predIdx == target) correct++
                total++
            }
        }
        val avgLoss = if (total > 0) totalLoss / total else 0f
        val acc = if (total > 0) correct.toFloat() / total else 0f
        return Pair(avgLoss, acc)
    }

    fun getModelParameters(): ModelParameters = model.getModelParameters()

    // --- 新規: ダウンロード済みモデルの自動検出と読み込み ---
    private fun attemptLoadDownloadedModel() {
        val filesDir = appFilesDir ?: File("data/data/com.example.hfl_experiment/files")
        val modelDir = File(filesDir, "model")
        if (!modelDir.exists()) {
            log("モデルディレクトリが存在しません: ${modelDir.absolutePath}")
            return
        }

        val candidateFiles = modelDir.listFiles()?.filter { it.isFile && it.name.endsWith(".bin") }
        if (candidateFiles.isNullOrEmpty()) {
            log("候補となるモデルファイルが見つかりませんでした。")
            return
        }

        // 最初の候補ファイルを使用
        val modelFile = candidateFiles[0]
        log("ダウンロード済みモデルファイルを検出: ${modelFile.absolutePath}")

        // ファイルからモデルを読み込む
        val success = loadFlatFromFile(modelFile.absolutePath)
        if (success) {
            log("モデルの読み込みに成功しました: ${modelFile.name}")
        } else {
            log("モデルの読み込みに失敗しました: ${modelFile.name}")
        }
    }

    // --- 新規: ファイルから flat 配列を読み込む汎用関数 ---
    private fun loadFlatFromFile(path: String): Boolean {
        return try {
            val file = File(path)
            if (!file.exists() || !file.isFile) {
                log("無効なファイルパス: $path")
                return false
            }

            // ファイルサイズを取得
            val fileSize = file.length()
            // Float32 バイナリとして読み込むためのバッファを準備
            val buffer = ByteBuffer.allocateDirect(fileSize.toInt()).order(ByteOrder.LITTLE_ENDIAN)
            // ファイルをバッファに読み込む
            file.inputStream().use { fis ->
                buffer.clear()
                fis.channel.use { channel ->
                    channel.read(buffer)
                }
            }
            buffer.rewind()

            // バッファから Float 配列を生成
            val floatArray = FloatArray((fileSize / 4).toInt()) // 4 バイトごとに Float を想定
            buffer.asFloatBuffer().get(floatArray)

            // モデルに読み込む
            model.loadFromFlat(floatArray, inputSize, hiddenSize, outputSize)
            log("モデルをファイルから正常に読み込みました。")
            true
        } catch (e: Exception) {
            log("ファイルからのモデル読み込み中にエラー: ${e.message}")
            false
        }
    }

    fun getSplitSizes(): Triple<Int, Int, Int> {
        val trainSamples = trainLoaderBatches.sumOf { it.size }
        val valSamples = validLoaderBatches.sumOf { it.size }
        val testSamples = testLoaderBatches.sumOf { it.size }
        return Triple(trainSamples, valSamples, testSamples)
    }
}
