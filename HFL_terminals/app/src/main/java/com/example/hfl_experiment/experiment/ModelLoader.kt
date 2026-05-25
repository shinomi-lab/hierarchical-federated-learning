package com.example.hfl_experiment.experiment

import android.content.Context
import android.util.Log
import com.example.hfl_experiment.training.WeightBinLoader
import org.json.JSONObject
import java.io.File
import kotlin.math.max
import kotlin.math.sqrt

/**
 * ModelLoader: loads a trained MLP model from meta.json + weight.bin and runs inference.
 *
 * Expected files at loadModel(path):
 *   - path          = weight.bin (binary weights in little-endian float32)
 *   - parent/meta.json = tensor layout description
 *   - path + ".norm.json" (optional) = {"mean": [...], "std": [...]} for input normalization
 *
 * Architecture: Linear(in→h) → LayerNorm → ReLU → Linear(h→h) → LayerNorm → ReLU → Linear(h→out)
 * Tensor names expected in meta.json (PyTorch state_dict format):
 *   layer1.weight, layer1.bias, norm1.weight, norm1.bias,
 *   layer2.weight, layer2.bias, norm2.weight, norm2.bias,
 *   layer3.weight, layer3.bias
 * Legacy names (weights1, biases1, etc.) are also supported as fallback.
 */
class ModelLoader {
    private val TAG = "ModelLoader"

    // Normalization params (optional)
    private var mean: FloatArray? = null
    private var std: FloatArray? = null

    // Loaded MLP weights
    private var weights1: FloatArray? = null   // [hiddenSize * inputSize]
    private var biases1: FloatArray? = null    // [hiddenSize]
    private var norm1Gamma: FloatArray? = null // [hiddenSize]
    private var norm1Beta: FloatArray? = null  // [hiddenSize]
    private var weights2: FloatArray? = null   // [hiddenSize * hiddenSize]
    private var biases2: FloatArray? = null    // [hiddenSize]
    private var norm2Gamma: FloatArray? = null // [hiddenSize]
    private var norm2Beta: FloatArray? = null  // [hiddenSize]
    private var weights3: FloatArray? = null   // [outputSize * hiddenSize]
    private var biases3: FloatArray? = null    // [outputSize]

    private var inputSize: Int = 0
    private var hiddenSize: Int = 0
    private var outputSize: Int = 0
    private var mlpLoaded = false

    fun loadModel(context: Context, path: String) {
        try {
            val binFile = File(path)
            if (!binFile.exists()) {
                Log.w(TAG, "weight.bin not found at $path; inference will use fallback heuristic")
                return
            }

            // Look for meta.json in same directory
            val metaFile = File(binFile.parentFile, "meta.json")
            if (!metaFile.exists()) {
                Log.w(TAG, "meta.json not found at ${metaFile.absolutePath}; inference will use fallback heuristic")
                return
            }

            val metaJson = JSONObject(metaFile.readText())
            val tensors = WeightBinLoader.loadAllTensors(metaJson, binFile)

            // PyTorch state_dict キー名 (layer1.weight 等) で読み込み、
            // 旧キー名 (weights1 等) にもフォールバック
            weights1   = tensors["layer1.weight"] ?: tensors["weights1"]
            biases1    = tensors["layer1.bias"]   ?: tensors["biases1"]
            norm1Gamma = tensors["norm1.weight"]  ?: tensors["norm1Gamma"]
            norm1Beta  = tensors["norm1.bias"]    ?: tensors["norm1Beta"]
            weights2   = tensors["layer2.weight"] ?: tensors["weights2"]
            biases2    = tensors["layer2.bias"]   ?: tensors["biases2"]
            norm2Gamma = tensors["norm2.weight"]  ?: tensors["norm2Gamma"]
            norm2Beta  = tensors["norm2.bias"]    ?: tensors["norm2Beta"]
            weights3   = tensors["layer3.weight"] ?: tensors["weights3"]
            biases3    = tensors["layer3.bias"]   ?: tensors["biases3"]

            // Infer architecture from tensor shapes
            val b1 = biases1
            val b3 = biases3
            val w1 = weights1
            if (b1 != null && b3 != null && w1 != null && b1.isNotEmpty() && b3.isNotEmpty()) {
                hiddenSize = b1.size
                outputSize = b3.size
                inputSize  = w1.size / hiddenSize
                mlpLoaded  = true
                Log.i(TAG, "ModelLoader: MLP loaded inputSize=$inputSize hiddenSize=$hiddenSize outputSize=$outputSize")
            } else {
                Log.w(TAG, "ModelLoader: missing required tensors; inference will use fallback heuristic")
            }

            // Load optional input normalization params
            val normFile = File(path + ".norm.json")
            if (normFile.exists()) {
                val txt = normFile.readText()
                Regex("\"mean\"\\s*:\\s*\\[(.*?)\\]").find(txt)?.groups?.get(1)?.value?.let { raw ->
                    mean = raw.split(',').map { it.trim().toFloat() }.toFloatArray()
                }
                Regex("\"std\"\\s*:\\s*\\[(.*?)\\]").find(txt)?.groups?.get(1)?.value?.let { raw ->
                    std = raw.split(',').map { it.trim().toFloat() }.toFloatArray()
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to load model: ${e.message}")
        }
    }

    /**
     * Runs MLP inference on the given input vector and returns raw logit scores per AP.
     * Falls back to a simple heuristic if the model is not loaded.
     */
    fun predict(input: FloatArray): FloatArray {
        val inp = input.copyOf()

        // Apply input normalization if available
        mean?.let { m ->
            val upTo = minOf(inp.size, m.size)
            for (i in 0 until upTo) inp[i] = inp[i] - m[i]
        }
        std?.let { s ->
            val upTo = minOf(inp.size, s.size)
            for (i in 0 until upTo) if (s[i] != 0f) inp[i] = inp[i] / s[i]
        }

        if (!mlpLoaded) {
            // Fallback heuristic when no model is available
            Log.w(TAG, "predict: model not loaded, using fallback heuristic")
            val tp = inp.getOrElse(0) { 1.0f }
            val rtt = inp.getOrElse(1) { 1.0f }
            return floatArrayOf(tp, tp * 0.9f - rtt * 0.001f)
        }

        return try {
            // Layer 1: Linear → LayerNorm → ReLU
            var h = linear(inp, weights1!!, biases1!!, inputSize, hiddenSize)
            h = layerNorm(h, norm1Gamma!!, norm1Beta!!)
            h = relu(h)

            // Layer 2: Linear → LayerNorm → ReLU
            h = linear(h, weights2!!, biases2!!, hiddenSize, hiddenSize)
            h = layerNorm(h, norm2Gamma!!, norm2Beta!!)
            h = relu(h)

            // Layer 3: Linear (output logits, no activation)
            linear(h, weights3!!, biases3!!, hiddenSize, outputSize)
        } catch (e: Exception) {
            Log.w(TAG, "predict: MLP forward pass failed: ${e.message}")
            val tp = inp.getOrElse(0) { 1.0f }
            val rtt = inp.getOrElse(1) { 1.0f }
            floatArrayOf(tp, tp * 0.9f - rtt * 0.001f)
        }
    }

    // --- MLP math helpers ---

    /** y[j] = sum_i(W[j*inDim+i] * x[i]) + b[j] */
    private fun linear(x: FloatArray, W: FloatArray, b: FloatArray, inDim: Int, outDim: Int): FloatArray {
        val y = FloatArray(outDim)
        for (j in 0 until outDim) {
            var sum = b[j]
            val offset = j * inDim
            for (i in 0 until inDim) sum += W[offset + i] * x[i]
            y[j] = sum
        }
        return y
    }

    /** Layer normalization: normalize then apply per-element gamma/beta. */
    private fun layerNorm(x: FloatArray, gamma: FloatArray, beta: FloatArray, eps: Float = 1e-5f): FloatArray {
        val n = x.size
        val mean = x.sum() / n
        val variance = x.fold(0f) { acc, v -> acc + (v - mean) * (v - mean) } / n
        val std = sqrt(variance + eps)
        return FloatArray(n) { i -> gamma[i] * (x[i] - mean) / std + beta[i] }
    }

    /** ReLU: max(0, x) element-wise. */
    private fun relu(x: FloatArray): FloatArray = FloatArray(x.size) { i -> max(0f, x[i]) }

    /** Release all loaded weight arrays to free memory. */
    fun release() {
        weights1 = null; biases1 = null
        norm1Gamma = null; norm1Beta = null
        weights2 = null; biases2 = null
        norm2Gamma = null; norm2Beta = null
        weights3 = null; biases3 = null
        mean = null; std = null
        mlpLoaded = false
    }
}
