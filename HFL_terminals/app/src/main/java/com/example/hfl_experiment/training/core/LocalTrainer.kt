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
    // 呼び出し元が渡す初期値（seed / 前サイクルの自モデル）。null ならダウンロード済みモデルを自動読込（I1）。
    private val initialParams: ModelParameters? = null,
) {
    companion object {
        /** ダウンロード済みモデルの置き場所（ModelUpdateUtil の保存先と一致させる）。 */
        fun downloadedModelFile(filesDir: File): File = File(File(filesDir, "model"), "weight.bin")
        private val TERMINAL_ORDER = listOf(
            "layer1.weight", "layer1.bias", "norm1.weight", "norm1.bias",
            "layer2.weight", "layer2.bias", "norm2.weight", "norm2.bias",
            "layer3.weight", "layer3.bias",
        )
    }

    /** 初期値の出所: PROVIDED / DOWNLOADED / RANDOM / RANDOM_LOAD_FAILED（init より前に宣言すること） */
    var initSource: String = "RANDOM"
        private set

    // --- 内部クラス: 各レイヤーとモデル ---
    private data class LayerOutput(val output: List<FloatArray>, val intermediates: Map<String, Any> = emptyMap())

    private inner class LinearLayer(val inputDim: Int, val outputDim: Int) {
        var weights = MutableList(outputDim) { MutableList(inputDim) { Random.nextFloat() * 2f / sqrt(inputDim.toFloat()) - 1f / sqrt(inputDim.toFloat()) } }
        var biases = MutableList(outputDim) { 0.0f }

        fun forward(inputs: List<FloatArray>): LayerOutput {
            val batchSize = inputs.size
            val outputs = List(batchSize) { FloatArray(outputDim) }
            for (b in 0 until batchSize) {
                for (j in 0 until outputDim) {
                    var sum = 0.0f
                    for (i in 0 until inputDim) {
                        sum += inputs[b][i] * weights[j][i]
                    }
                    outputs[b][j] = sum + biases[j]
                }
            }
            return LayerOutput(outputs, mapOf("inputs" to inputs))
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

        fun forward(inputs: List<FloatArray>): LayerOutput {
            val batchSize = inputs.size
            val outputs = List(batchSize) { FloatArray(dim) }
            val means = FloatArray(batchSize)
            val variances = FloatArray(batchSize)
            val inputsNormalized = List(batchSize) { FloatArray(dim) }

            for (b in 0 until batchSize) {
                val mean = inputs[b].average().toFloat()
                means[b] = mean
                val variance = inputs[b].map { (it - mean) * (it - mean) }.average().toFloat()
                variances[b] = variance
                val stdInv = 1f / sqrt(variance + epsilon)
                for (i in 0 until dim) {
                    val xn = (inputs[b][i] - mean) * stdInv
                    inputsNormalized[b][i] = xn
                    outputs[b][i] = xn * gamma[i] + beta[i]
                }
            }
            // x_norm は FloatArray のリスト、inv_std は List<Float> で保存する
            return LayerOutput(
                outputs,
                mapOf(
                    "inputs" to inputs,
                    "inv_std" to means.indices.map { 1f / sqrt(variances[it] + epsilon) },
                    "x_norm" to inputsNormalized
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
                // dVar = Σ dx̂·(x−μ)·(−½)σ⁻³（x̂ ではなく x−μ。x̂ を使うと σ⁻¹ 倍ずれて下層の勾配がすべて誤る, F7）
                val dVar = dxNorm.indices.sumOf { i -> (dxNorm[i] * (inputs[b][i] - mean)).toDouble() } * -0.5 * inv3
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
        fun forward(inputs: List<FloatArray>): LayerOutput {
            val outputs = inputs.map { it.map { x -> max(0f, x) }.toFloatArray() }
            return LayerOutput(outputs, mapOf("inputs" to inputs))
        }
        fun backward(dOutputs: List<FloatArray>, intermediates: Map<String, Any>): List<FloatArray> {
            val inputs = intermediates["inputs"] as List<FloatArray>
            return dOutputs.zip(inputs).map { (d, inp) ->
                d.zip(inp).map { (d_val, i_val) -> if (i_val > 0) d_val else 0f }.toFloatArray()
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

            // リストを差し替えると optimizer の参照が切れるため、既存リストへ値をコピーする（I4）
            copyMat(layer1.weights, mp.weights1); copyVec(layer1.biases, mp.biases1)
            copyVec(norm1.gamma, mp.norm1Gamma); copyVec(norm1.beta, mp.norm1Beta)
            copyMat(layer2.weights, mp.weights2); copyVec(layer2.biases, mp.biases2)
            copyVec(norm2.gamma, mp.norm2Gamma); copyVec(norm2.beta, mp.norm2Beta)
            copyMat(layer3.weights, mp.weights3); copyVec(layer3.biases, mp.biases3)
        }

        private fun copyVec(dst: MutableList<Float>, src: List<Float>) {
            require(dst.size == src.size) { "vector size mismatch: expected ${dst.size}, got ${src.size}" }
            for (i in dst.indices) dst[i] = src[i]
        }

        private fun copyMat(dst: MutableList<MutableList<Float>>, src: List<List<Float>>) {
            require(dst.size == src.size && dst.indices.all { dst[it].size == src[it].size }) { "matrix shape mismatch" }
            for (r in dst.indices) for (c in dst[r].indices) dst[r][c] = src[r][c]
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
        if (initialParams != null) {
            model.loadFromModelParameters(initialParams)
            initSource = "PROVIDED"
        } else {
            attemptLoadDownloadedModel() // 自動検出を初期化時に実行（I1）
        }
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
                val loss = trainOnBatch(inputs, targets)
                epochLossAcc += loss * targets.size
                epochSamples += targets.size
            }

            // Validation at epoch end
            val (valLoss, valAcc) = inferenceOnBatches(validLoaderBatches)
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

    /** 1バッチの学習。毎回 trainMode にする（エポック末の検証で evalMode のままだと中間値が保存されず勾配が誤るため, I6）。 */
    internal fun trainOnBatch(inputs: List<FloatArray>, targets: List<Int>): Float {
        model.trainMode()
        val logits = model.forward(inputs)
        val (loss, dlogits) = CrossEntropyLoss.compute(logits, targets)
        val grads = model.backward(dlogits)
        try {
            optimizer.step(grads, clipMaxNorm)
        } catch (e: Exception) {
            RealTimeLogger.w("LocalTrainer", "Optimizer step failed: ${e.message}")
        }
        scheduler.step()
        return loss
    }

    internal fun lossForTest(inputs: List<FloatArray>, targets: List<Int>): Float =
        CrossEntropyLoss.compute(forwardForTest(inputs), targets).first

    internal fun gradientsForTest(inputs: List<FloatArray>, targets: List<Int>): Map<String, FloatArray> {
        model.trainMode()
        val (_, dlogits) = CrossEntropyLoss.compute(model.forward(inputs), targets)
        val g = model.backward(dlogits)
        fun w(k: String) = g.getValue(k).dWeights.flatten().toFloatArray()
        fun b(k: String) = g.getValue(k).dBiases.toFloatArray()
        return TERMINAL_ORDER.zip(listOf(
            w("l1"), b("l1"), b("n1_g"), b("n1_b"), w("l2"), b("l2"), b("n2_g"), b("n2_b"), w("l3"), b("l3"),
        )).toMap()
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

    // --- ダウンロード済みモデルの自動検出と読み込み（I1/I3） ---
    private fun attemptLoadDownloadedModel() {
        val filesDir = appFilesDir ?: File("data/data/com.example.hfl_experiment/files")
        val bin = downloadedModelFile(filesDir)
        if (!bin.isFile) {
            log("ダウンロード済みモデルがありません: ${bin.absolutePath}（ランダム初期化）")
            initSource = "RANDOM"
            return
        }
        try {
            val meta = File(bin.parentFile, "meta.json")
            val tensors = if (meta.isFile) {
                com.example.hfl_experiment.training.WeightBinLoader.loadAllTensors(org.json.JSONObject(meta.readText()), bin)
            } else {
                readInConverterOrder(bin)
            }
            model.loadFromModelParameters(paramsFromNamed(tensors))
            initSource = "DOWNLOADED"
            log("ダウンロード済みモデルを読み込みました: ${bin.name} (meta=${meta.isFile})")
        } catch (e: Exception) {
            initSource = "RANDOM_LOAD_FAILED"
            RealTimeLogger.e("LocalTrainer", "ダウンロード済みモデルの読み込みに失敗（ランダム初期化で続行）: ${e.message}")
            log("ダウンロード済みモデルの読み込みに失敗: ${e.message}")
        }
    }

    /** meta 無しの bin を、変換スクリプト(pt_to_meta_weights.py)の固定順＝キー名ソート順で読む。 */
    private fun readInConverterOrder(bin: File): Map<String, FloatArray> {
        val sizes = mapOf(
            "layer1.weight" to hiddenSize * inputSize, "layer1.bias" to hiddenSize,
            "norm1.weight" to hiddenSize, "norm1.bias" to hiddenSize,
            "layer2.weight" to hiddenSize * hiddenSize, "layer2.bias" to hiddenSize,
            "norm2.weight" to hiddenSize, "norm2.bias" to hiddenSize,
            "layer3.weight" to outputSize * hiddenSize, "layer3.bias" to outputSize,
        )
        val bb = ByteBuffer.wrap(bin.readBytes()).order(ByteOrder.LITTLE_ENDIAN)
        require(bb.remaining() == sizes.values.sum() * 4) { "bin size ${bb.remaining()} != expected ${sizes.values.sum() * 4}" }
        val fb = bb.asFloatBuffer()
        return sizes.keys.sorted().associateWith { k -> FloatArray(sizes.getValue(k)).also { fb.get(it) } }
    }

    /** テンソル名 → ModelParameters。欠落・要素数不一致は IllegalArgumentException。 */
    private fun paramsFromNamed(t: Map<String, FloatArray>): ModelParameters {
        fun get(name: String, n: Int): FloatArray {
            val a = t[name] ?: throw IllegalArgumentException("tensor '$name' missing")
            require(a.size == n) { "tensor '$name' size ${a.size} != expected $n" }
            return a
        }
        fun rows(a: FloatArray, r: Int, c: Int) = List(r) { i -> List(c) { j -> a[i * c + j] } }
        val h = hiddenSize
        return ModelParameters(
            weights1 = rows(get("layer1.weight", h * inputSize), h, inputSize), biases1 = get("layer1.bias", h).toList(),
            norm1Gamma = get("norm1.weight", h).toList(), norm1Beta = get("norm1.bias", h).toList(),
            weights2 = rows(get("layer2.weight", h * h), h, h), biases2 = get("layer2.bias", h).toList(),
            norm2Gamma = get("norm2.weight", h).toList(), norm2Beta = get("norm2.bias", h).toList(),
            weights3 = rows(get("layer3.weight", outputSize * h), outputSize, h), biases3 = get("layer3.bias", outputSize).toList(),
        )
    }

    internal fun forwardForTest(inputs: List<FloatArray>): List<FloatArray> {
        model.evalMode()
        return model.forward(inputs)
    }

    fun getSplitSizes(): Triple<Int, Int, Int> {
        val trainSamples = trainLoaderBatches.sumOf { it.size }
        val valSamples = validLoaderBatches.sumOf { it.size }
        val testSamples = testLoaderBatches.sumOf { it.size }
        return Triple(trainSamples, valSamples, testSamples)
    }
}
