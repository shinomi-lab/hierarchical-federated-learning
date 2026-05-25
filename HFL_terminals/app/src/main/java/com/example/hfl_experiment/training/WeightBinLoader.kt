package com.example.hfl_experiment.training

import com.example.hfl_experiment.util.logging.RealTimeLogger
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.security.MessageDigest

/**
 * Simple helper to load weight.bin according to meta.json and return a map of tensor name to FloatArray.
 * This is best-effort and synchronous; call from IO dispatcher.
 */
@Suppress("unused")
object WeightBinLoader {
    private const val TAG = "WeightBinLoader"

    @Suppress("unused")
    data class TensorEntry(val name: String, val shape: List<Int>, val offset: Long, val lengthBytes: Int, val dtype: String?, val order: String?)

    fun verifySha256(file: File, expectedHex: String): Boolean {
        return try {
            val md = MessageDigest.getInstance("SHA-256")
            val buf = ByteArray(8 * 1024)
            file.inputStream().use { fis ->
                var r = fis.read(buf)
                while (r > 0) {
                    md.update(buf, 0, r)
                    r = fis.read(buf)
                }
            }
            val got = md.digest().joinToString("") { "%02x".format(it) }
            got.equals(expectedHex, ignoreCase = true)
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "verifySha256 failed: ${e.message}")
            false
        }
    }

    fun loadAllTensors(metaJson: JSONObject, binFile: File, memoryOptEnabled: Boolean = true): Map<String, FloatArray> {
        val out = mutableMapOf<String, FloatArray>()
        try {
            val tensors = metaJson.getJSONArray("tensors")
            val dtype = metaJson.optString("dtype", "float32")
            val endian = metaJson.optString("endianness", "little")
            if (dtype != "float32") RealTimeLogger.w(TAG, "loadAllTensors: dtype is $dtype; only float32 supported in loader")

            // Pre-scan to find max tensor size, then allocate one reusable buffer
            var maxBytes = 0
            if (memoryOptEnabled) {
                for (i in 0 until tensors.length()) {
                    val lb = tensors.getJSONObject(i).optInt("length_bytes", 0)
                    if (lb > maxBytes) maxBytes = lb
                }
            }
            val reusableBuf = if (memoryOptEnabled && maxBytes > 0) ByteArray(maxBytes) else null

            RandomAccessFile(binFile, "r").use { raf ->
                for (i in 0 until tensors.length()) {
                    val t = tensors.getJSONObject(i)
                    val name = t.getString("name")
                    val shapeArr = t.getJSONArray("shape")
                    val shape = List(shapeArr.length()) { idx -> shapeArr.getInt(idx) }
                    val offset = t.getLong("offset")
                    val lengthBytes = t.getInt("length_bytes")
                    // bounds check
                    if (offset < 0 || lengthBytes <= 0) {
                        RealTimeLogger.w(TAG, "tensor $name has invalid offset/length")
                        continue
                    }
                    raf.seek(offset)
                    val buf = reusableBuf ?: ByteArray(lengthBytes)
                    raf.readFully(buf, 0, lengthBytes)
                    val bb = ByteBuffer.wrap(buf, 0, lengthBytes)
                    if (endian == "little") bb.order(ByteOrder.LITTLE_ENDIAN) else bb.order(ByteOrder.BIG_ENDIAN)
                    val fb: FloatBuffer = bb.asFloatBuffer()
                    val arr = FloatArray(fb.remaining())
                    fb.get(arr)
                    // reshape is left to consumer; we return flat array
                    out[name] = arr
                    RealTimeLogger.i(TAG, "loaded tensor=$name size=${arr.size} shape=${shape.joinToString(",")}")
                }
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "loadAllTensors failed: ${e.message}")
        }
        return out
    }
}
