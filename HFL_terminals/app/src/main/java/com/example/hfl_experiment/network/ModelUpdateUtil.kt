package com.example.hfl_experiment.network

import android.content.Context
import com.example.hfl_experiment.util.logging.RealTimeLogger
import com.example.hfl_experiment.AppConfig
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.security.MessageDigest
import android.content.Intent
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.delay
import org.json.JSONObject

object ModelUpdateUtil {
    private const val TAG = "ModelUpdateUtil"
    // simple in-process lock to avoid concurrent downloads
    private val downloadLock = AtomicBoolean(false)
    const val ACTION_MODEL_UPDATED = "com.example.hfl_experiment.ACTION_MODEL_UPDATED"

    private fun tryAcquire(): Boolean = downloadLock.compareAndSet(false, true)
    private fun release() { downloadLock.set(false) }

    // Backup helper: copy current model to .bak
    private fun backupModel(context: Context, modelFile: File): File? {
        if (!modelFile.exists()) return null
        val backup = File(context.filesDir, modelFile.name + ".bak")
        return try {
            modelFile.copyTo(backup, overwrite = true)
            RealTimeLogger.i(TAG, "Backed up model to ${backup.absolutePath}")
            backup
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "Failed to backup model: ${e.message}")
            null
        }
    }

    // Restore helper: copy .bak back to modelFile
    private fun restoreModel(context: Context, modelFile: File, backup: File?) {
        if (backup == null || !backup.exists()) return
        try {
            backup.copyTo(modelFile, overwrite = true)
            RealTimeLogger.i(TAG, "Restored model from ${backup.absolutePath}")
        } catch (e: Exception) {
            RealTimeLogger.e(TAG, "Failed to restore model: ${e.message}")
        }
    }

    /**
     * 新仕様: meta.json と weight.bin の両方をダウンロード・検証して適用する。
     * metaUrl が null の場合は binUrl のみで旧方式のダウンロードを行う。
     */
    suspend fun downloadAndApplyModelSet(
        context: Context,
        downloadBaseUrl: String,
        client: OkHttpClient,
        authToken: String?,
        binRel: String?,
        metaRel: String?,
        expectedSha: String? = null,
        expectedSize: Long? = null
    ): Pair<Boolean, String> {
        if (!tryAcquire()) return Pair(false, "lock")
        try {
            val targetModelFile = File(context.filesDir, "global_model.pt")
            val backup = backupModel(context, targetModelFile)

            // 1. Meta (JSON) のダウンロードと検証準備
            var expectedSha256: String? = null
            var expectedSizeFromMeta: Long? = -1L

            if (!metaRel.isNullOrBlank()) {
                val metaFile = File(context.cacheDir, "model_meta.json")
                val metaUrl = buildDownloadUrl(downloadBaseUrl, metaRel)
                
                // Metaダウンロード
                val metaDl = downloadFile(context, metaUrl, client, authToken, metaFile)
                if (!metaDl.first) {
                    RealTimeLogger.w(TAG, "Failed to download meta: ${metaDl.second}. Continuing with bin only if possible.")
                } else {
                    try {
                        val jsonStr = metaFile.readText()
                        val jo = JSONObject(jsonStr)
                        // extract sha256
                        val sha = jo.optString("weights_sha256") ?: jo.optString("sha256")
                        if (!sha.isNullOrBlank()) expectedSha256 = sha
                        // extract size
                        val sz = jo.optLong("weights_size", -1L)
                        if (sz > 0) expectedSizeFromMeta = sz
                        RealTimeLogger.i(TAG, "Meta parsed: sha=$expectedSha256 size=$expectedSizeFromMeta")
                    } catch (e: Exception) {
                        RealTimeLogger.w(TAG, "Failed to parse meta json: ${e.message}")
                    }
                }
            }

            // 2. Binary のダウンロード
            if (binRel.isNullOrBlank()) {
                restoreModel(context, targetModelFile, backup)
                return Pair(false, "No binary path provided")
            }

            val binUrl = buildDownloadUrl(downloadBaseUrl, binRel)
            val binTemp = File(context.cacheDir, "model_bin.tmp")
            
            val binDl = downloadFile(context, binUrl, client, authToken, binTemp)
            if (!binDl.first) {
                restoreModel(context, targetModelFile, backup)
                return Pair(false, "Binary download failed: ${binDl.second}")
            }

            // 3. 検証 (SHA256 & Size)
            val useExpectedSize = if (expectedSize != null && expectedSize > 0) expectedSize else if (expectedSizeFromMeta != null && expectedSizeFromMeta > 0) expectedSizeFromMeta else -1L
            if (useExpectedSize > 0) {
                if (binTemp.length() != useExpectedSize) {
                    val msg = "Size mismatch: expected=$useExpectedSize got=${binTemp.length()}"
                    RealTimeLogger.e(TAG, msg)
                    binTemp.delete()
                    restoreModel(context, targetModelFile, backup)
                    return Pair(false, msg)
                }
            }
            val useExpectedSha = expectedSha ?: expectedSha256
            if (!useExpectedSha.isNullOrBlank()) {
                val digest = MessageDigest.getInstance("SHA-256")
                val fileBytes = binTemp.readBytes()
                val fileHash = digest.digest(fileBytes).joinToString("") { "%02x".format(it) }
                // normalize server hash (remove "sha256:" prefix if present)
                val cleanExpected = useExpectedSha.split(":").last()
                if (!fileHash.equals(cleanExpected, ignoreCase = true)) {
                    val msg = "SHA256 mismatch: expected=$cleanExpected got=$fileHash"
                    RealTimeLogger.e(TAG, msg)
                    binTemp.delete()
                    restoreModel(context, targetModelFile, backup)
                    return Pair(false, msg)
                }
            }

            // 4. 適用
            try {
                binTemp.copyTo(targetModelFile, overwrite = true)
                binTemp.delete()
                RealTimeLogger.d(TAG, "Model updated successfully to ${targetModelFile.absolutePath}")
                
                // Broadcast
                val it = Intent(ACTION_MODEL_UPDATED).apply {
                    putExtra("path", targetModelFile.absolutePath)
                    putExtra("ts", System.currentTimeMillis())
                }
                context.sendBroadcast(it)
                
                return Pair(true, "ok")
            } catch (e: Exception) {
                RealTimeLogger.e(TAG, "Apply failed: ${e.message}")
                restoreModel(context, targetModelFile, backup)
                return Pair(false, "apply failed: ${e.message}")
            }

        } catch (e: Exception) {
            RealTimeLogger.e(TAG, "downloadAndApplyModelSet exception: ${e.message}")
            return Pair(false, e.message ?: "unknown error")
        } finally {
            release()
        }
    }

    // Helper: build absolute URL handling relative paths and query params
    private fun buildDownloadUrl(base: String, rel: String): String {
        if (rel.startsWith("http://") || rel.startsWith("https://")) return rel
        val baseTrim = base.trimEnd('/')
        // encode rel path components if needed, but here assuming simple path or pre-encoded
        // Simple query param append for legacy compat
        return if (baseTrim.contains("?")) "$baseTrim&rel_path=$rel" else "$baseTrim?rel_path=$rel"
    }

    // Helper: single file download with retries
    private fun downloadFile(context: Context, url: String, client: OkHttpClient, authToken: String?, destFile: File): Pair<Boolean, String> {
        var attempt = 0
        val maxRetries = 3
        while (attempt < maxRetries) {
            attempt++
            try {
                val reqBuilder = Request.Builder().url(url)
                if (!authToken.isNullOrBlank()) reqBuilder.header("Authorization", "Bearer $authToken")
                val response = client.newCall(reqBuilder.build()).execute()
                
                if (response.isSuccessful) {
                    response.body?.let { body -> destFile.outputStream().use { output -> body.byteStream().copyTo(output) } }
                    response.close()
                    appendLog(context, "DL_OK\t${System.currentTimeMillis()}\t$url\t${destFile.length()}")
                    return Pair(true, "ok")
                } else {
                    val code = response.code
                    response.close()
                    RealTimeLogger.w(TAG, "Download failed code=$code url=$url")
                    if (code == 404 || code == 403) return Pair(false, "HTTP $code") // no retry for 404/403
                }
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "Download error: ${e.message}")
            }
            try { Thread.sleep(1000) } catch (_: Exception) {}
        }
        return Pair(false, "Max retries reached")
    }

    // Legacy method for compatibility (delegates to new one with null meta)
    suspend fun downloadAndApplyModel(context: Context, downloadBaseUrl: String, client: OkHttpClient, authToken: String?, downloadRel: String, expectedSha256: String?): Pair<Boolean, String> {
        // Adjust .pt -> .bin here for legacy calls too
        val adjustedRel = if (downloadRel.endsWith(".pt")) downloadRel.replace(".pt", ".bin") else downloadRel
        return downloadAndApplyModelSet(context, downloadBaseUrl, client, authToken, adjustedRel, null)
    }

    /**
     * Wrapper for lock-protected download from URL (legacy)
     */
    fun downloadAndApplyModelWithLock(context: Context, downloadBaseUrl: String, client: OkHttpClient, authToken: String?, downloadRel: String, expectedSha256: String?): Pair<Boolean, String> {
        return kotlinx.coroutines.runBlocking {
             downloadAndApplyModel(context, downloadBaseUrl, client, authToken, downloadRel, expectedSha256)
        }
    }
    
    // Wrapper for absolute URL (legacy)
    fun downloadAndApplyModelFromUrlWithLock(context: Context, url: String, client: OkHttpClient, authToken: String?, expectedSha256: String?): Pair<Boolean, String> {
        return kotlinx.coroutines.runBlocking {
            // treat absolute url as binRel, no meta
            downloadAndApplyModelSet(context, "", client, authToken, url, null)
        }
    }

    private fun appendLog(context: Context, line: String) {
        try {
            val logF = File(context.filesDir, "model_update_log.txt")
            logF.appendText(line + "\n")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "failed to append log: ${e.message}")
        }
    }
}
