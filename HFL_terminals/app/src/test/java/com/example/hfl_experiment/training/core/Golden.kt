package com.example.hfl_experiment.training.core

import com.google.gson.Gson
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import kotlin.math.abs

/** ゴールデンフィクスチャ（src/test/resources/golden）の読込と許容誤差比較。 */
object Golden {
    fun json(path: String): JsonObject {
        val url = Golden::class.java.classLoader!!.getResource(path) ?: error("fixture not found: $path")
        return Gson().fromJson(url.readText(), JsonObject::class.java)
    }
    fun floats(a: JsonArray) = FloatArray(a.size()) { a[it].asFloat }
    fun rows(a: JsonArray): List<FloatArray> = List(a.size()) { floats(a[it].asJsonArray) }
    fun named(o: JsonObject): Map<String, FloatArray> = o.keySet().associateWith { floats(o.getAsJsonArray(it)) }

    fun toParams(t: Map<String, FloatArray>, i: Int, h: Int, o: Int): ModelParameters {
        fun rows(a: FloatArray, r: Int, c: Int) = List(r) { x -> List(c) { y -> a[x * c + y] } }
        return ModelParameters(
            weights1 = rows(t.getValue("layer1.weight"), h, i), biases1 = t.getValue("layer1.bias").toList(),
            norm1Gamma = t.getValue("norm1.weight").toList(), norm1Beta = t.getValue("norm1.bias").toList(),
            weights2 = rows(t.getValue("layer2.weight"), h, h), biases2 = t.getValue("layer2.bias").toList(),
            norm2Gamma = t.getValue("norm2.weight").toList(), norm2Beta = t.getValue("norm2.bias").toList(),
            weights3 = rows(t.getValue("layer3.weight"), o, h), biases3 = t.getValue("layer3.bias").toList(),
        )
    }

    fun flat(p: ModelParameters): Map<String, FloatArray> = linkedMapOf(
        "layer1.weight" to p.weights1.flatten().toFloatArray(), "layer1.bias" to p.biases1.toFloatArray(),
        "norm1.weight" to p.norm1Gamma.toFloatArray(), "norm1.bias" to p.norm1Beta.toFloatArray(),
        "layer2.weight" to p.weights2.flatten().toFloatArray(), "layer2.bias" to p.biases2.toFloatArray(),
        "norm2.weight" to p.norm2Gamma.toFloatArray(), "norm2.bias" to p.norm2Beta.toFloatArray(),
        "layer3.weight" to p.weights3.flatten().toFloatArray(), "layer3.bias" to p.biases3.toFloatArray(),
    )

    fun assertClose(msg: String, expected: FloatArray, actual: FloatArray, rel: Float = 1e-4f, absTol: Float = 1e-6f) {
        assertEquals("$msg: size", expected.size, actual.size)
        for (i in expected.indices) {
            val tol = absTol + rel * abs(expected[i])
            assertTrue("$msg[$i]: expected=${expected[i]} actual=${actual[i]}", abs(expected[i] - actual[i]) <= tol)
        }
    }
}
