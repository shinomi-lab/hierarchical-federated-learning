package com.example.hfl_experiment.training

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.security.MessageDigest

object ModelWeightsLoader {
    private const val TAG = "ModelWeightsLoader"

    data class TensorMeta(
        val name: String,
        val shape: IntArray,
        val offset: Long,
        val lengthBytes: Int,
        val dtype: String = "float32",
        val order: String = "C"
    )

    data class Meta(
        val formatVersion: Int,
        val modelVersion: String,
        val dtype: String,
        val endianness: String,
        val weightsSize: Long,
        val weightsSha256: String,
        val tensors: List<TensorMeta>
    )

    // parse meta.json into Meta data class
    fun parseMetaJson(jsonText: String): Meta {
        val j = JSONObject(jsonText)
        val formatVersion = j.getInt("format_version")
        val modelVersion = j.optString("model_version", "")
        val dtype = j.optString("dtype", "float32")
        val endianness = j.optString("endianness", "little")
        val weightsSize = j.optLong("weights_size", -1)
        val weightsSha256 = j.optString("weights_sha256", "")
        val tensorsJson = j.getJSONArray("tensors")
        val tensors = mutableListOf<TensorMeta>()
        for (i in 0 until tensorsJson.length()) {
            val t = tensorsJson.getJSONObject(i)
            val name = t.getString("name")
            val shapeArr = t.getJSONArray("shape")
            val shape = IntArray(shapeArr.length()) { idx -> shapeArr.getInt(idx) }
            val offset = t.getLong("offset")
            val lengthBytes = t.getInt("length_bytes")
            val tdtype = t.optString("dtype", dtype)
            val order = t.optString("order", "C")
            tensors.add(TensorMeta(name, shape, offset, lengthBytes, tdtype, order))
        }
        return Meta(formatVersion, modelVersion, dtype, endianness, weightsSize, weightsSha256, tensors)
    }

    // compute sha256 of file
    suspend fun sha256OfFile(file: File): String = withContext(Dispatchers.IO) {
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { fis ->
            val buf = ByteArray(65536)
            var read = fis.read(buf)
            while (read >= 0) {
                if (read > 0) md.update(buf, 0, read)
                read = fis.read(buf)
            }
        }
        return@withContext md.digest().joinToString("") { String.format("%02x", it) }
    }

    // Load weights into map: name -> FloatArray
    // Only supports float32 little-endian for now
    suspend fun loadWeights(meta: Meta, weightBinFile: File): Map<String, FloatArray> = withContext(Dispatchers.IO) {
        Log.i(TAG, "loadWeights: verifying weight file size=${weightBinFile.length()} expected=${meta.weightsSize}")
        if (meta.weightsSize >= 0 && weightBinFile.length() != meta.weightsSize) {
            Log.w(TAG, "loadWeights: size mismatch: file=${weightBinFile.length()} expected=${meta.weightsSize}")
        }
        if (meta.weightsSha256.isNotBlank()) {
            try {
                val sha = sha256OfFile(weightBinFile)
                if (!sha.equals(meta.weightsSha256, ignoreCase = true)) {
                    Log.w(TAG, "loadWeights: sha256 mismatch: actual=$sha expected=${meta.weightsSha256}")
                } else {
                    Log.i(TAG, "loadWeights: sha256 verified ok")
                }
            } catch (e: Exception) {
                Log.w(TAG, "loadWeights: sha256 compute failed: ${e.message}")
            }
        }

        val raf = RandomAccessFile(weightBinFile, "r")
        val result = mutableMapOf<String, FloatArray>()
        try {
            for (t in meta.tensors) {
                // only support float32
                if (t.dtype != "float32") {
                    Log.w(TAG, "loadWeights: tensor ${t.name} has dtype ${t.dtype} (unsupported)")
                    continue
                }
                if (t.lengthBytes % 4 != 0) {
                    Log.w(TAG, "loadWeights: tensor ${t.name} lengthBytes not multiple of 4: ${t.lengthBytes}")
                    continue
                }
                val numElements = t.lengthBytes / 4
                val bytes = ByteArray(t.lengthBytes)
                raf.seek(t.offset)
                raf.readFully(bytes)
                val bb = ByteBuffer.wrap(bytes)
                bb.order(if (meta.endianness == "big") ByteOrder.BIG_ENDIAN else ByteOrder.LITTLE_ENDIAN)
                val fb: FloatBuffer = bb.asFloatBuffer()
                val arr = FloatArray(fb.remaining())
                fb.get(arr)
                result[t.name] = arr
                Log.d(TAG, "loadWeights: loaded tensor ${t.name} elems=${arr.size} shape=${t.shape.contentToString()}")
            }
        } finally {
            try { raf.close() } catch (_: Exception) {}
        }
        return@withContext result
    }
}

