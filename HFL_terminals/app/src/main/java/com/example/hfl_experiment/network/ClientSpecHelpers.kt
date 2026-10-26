package com.example.hfl_experiment.network

import android.content.Context
import com.example.hfl_experiment.util.logging.RealTimeLogger
import kotlinx.coroutines.delay
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import kotlin.random.Random
import kotlinx.serialization.json.*

/**
 * Helpers implementing behaviors described in ANDROID_CLIENT_SPEC_SUMMARY_v1.1.md
 * - exponential backoff with Retry-After support
 * - WebSocket notification handler (dedupe, delay_hint_ms, download + ACK)
 * - multipart log upload with 413/429 handling
 * - ACK sender helper
 */
object ClientSpecHelpers {
    private const val TAG = "ClientSpecHelpers"

    // SharedPreferences key prefix for processed notification IDs
    private const val PREF_WS_NOTIF = "ws_notifications"

    /**
     * Respect Retry-After if present (in seconds). Otherwise use exponential backoff with jitter.
     * Attempts up to maxAttempts. The action lambda should perform the HTTP call and return the Response.
     * If the response code is 429 or 413 and contains Retry-After, this function will wait and retry.
     */
    suspend fun retryHttpWithBackoff(
        maxAttempts: Int = 4,
        initialBackoffMs: Long = 500L,
        maxBackoffMs: Long = 60_000L,
        action: suspend () -> Response
    ): Response {
        var attempt = 0
        var backoff = initialBackoffMs
        while (true) {
            attempt++
            try {
                val resp = withContext(Dispatchers.IO) { action() }
                if (resp.code == 429 || resp.code == 413) {
                    // Respect Retry-After header if present (seconds)
                    val retryAfter = parseRetryAfterSeconds(resp.header("Retry-After"))
                    resp.close()
                    if (retryAfter != null) {
                        RealTimeLogger.w(TAG, "Server asked Retry-After=${retryAfter}s (code=${resp.code}), sleeping")
                        delay(retryAfter * 1000L)
                    } else {
                        // no header: exponential backoff
                        if (attempt >= maxAttempts) return resp
                        val jitter = Random.nextLong(0, backoff / 2 + 1)
                        val wait = (backoff + jitter).coerceAtMost(maxBackoffMs)
                        RealTimeLogger.w(TAG, "HTTP ${resp.code} without Retry-After; waiting ${wait}ms (attempt=$attempt)")
                        delay(wait)
                        backoff = (backoff * 2).coerceAtMost(maxBackoffMs)
                    }
                    // continue to retry
                    continue
                }
                // success or other codes: return and let caller interpret
                return resp
            } catch (e: Exception) {
                // network failure: backoff and retry
                if (attempt >= maxAttempts) throw e
                val jitter = Random.nextLong(0, backoff / 2 + 1)
                val wait = (backoff + jitter).coerceAtMost(maxBackoffMs)
                RealTimeLogger.w(TAG, "Network call failed: ${e.message}. Backing off ${wait}ms (attempt=$attempt)")
                delay(wait)
                backoff = (backoff * 2).coerceAtMost(maxBackoffMs)
            }
        }
    }

    private fun parseRetryAfterSeconds(header: String?): Long? {
        if (header.isNullOrBlank()) return null
        return try {
            header.trim().toLong()
        } catch (_: Exception) {
            null
        }
    }

    // ---------- WebSocket notification handler ----------

    /**
     * Handle a notification payload (as raw JSON string) according to the spec:
     * - deduplicate by notification_id
     * - apply delay_hint_ms randomized +/-20%
     * - trigger download (absolute download_url preferred, otherwise download_rel against baseUrl)
     * - send ACK after successful apply
     *
     * This function is suspend and should be launched from a coroutine scope.
     */
    suspend fun handleWsNotification(
        context: Context,
        payloadJson: String,
        baseUrl: String,
        authToken: String?,
        client: OkHttpClient = OkHttpClient()
    ) {
        // parse minimal fields using kotlinx.serialization.json
        try {
            val jElem = Json.parseToJsonElement(payloadJson)
            if (jElem !is JsonObject) {
                RealTimeLogger.w(TAG, "WS payload not a JSON object")
                return
            }
            val j = jElem
            val schemaVersion = j["schema_version"]?.jsonPrimitive?.intOrNull ?: 1
            if (schemaVersion != 1) {
                RealTimeLogger.w(TAG, "Unsupported schema_version=$schemaVersion")
                return
            }
            val notifId = j["notification_id"]?.jsonPrimitive?.contentOrNull
            if (notifId.isNullOrBlank()) {
                RealTimeLogger.w(TAG, "Empty notification_id; ignoring")
                return
            }

            // dedupe using SharedPreferences
            val prefs = context.getSharedPreferences(PREF_WS_NOTIF, Context.MODE_PRIVATE)
            val key = notifKey(notifId)
            if (prefs.contains(key)) {
                RealTimeLogger.i(TAG, "Notification $notifId already processed; skipping")
                return
            }

            // mark as in-progress immediately to avoid duplicate concurrent handling
            prefs.edit().putBoolean(key, true).apply()

            val delayHintMs = j["delay_hint_ms"]?.jsonPrimitive?.longOrNull ?: 0L
            val randomized = randomizeDelay(delayHintMs)
            if (randomized > 0) {
                RealTimeLogger.d(TAG, "Waiting randomized delay ${randomized}ms for notif=$notifId")
                delay(randomized)
            }

            // resolve URL
            val downloadUrl = j["download_url"]?.jsonPrimitive?.contentOrNull
            val downloadRel = j["download_rel"]?.jsonPrimitive?.contentOrNull
            val round = j["round"]?.jsonPrimitive?.intOrNull
            var applied = false
            var msg = ""

            if (!downloadUrl.isNullOrBlank()) {
                // absolute URL
                val res = withContext(Dispatchers.IO) {
                    ModelUpdateUtil.downloadAndApplyModelFromUrlWithLock(context, downloadUrl, client, authToken, null)
                }
                applied = res.first
                msg = res.second
            } else if (!downloadRel.isNullOrBlank()) {
                val res = withContext(Dispatchers.IO) {
                    ModelUpdateUtil.downloadAndApplyModelWithLock(context, baseUrl, client, authToken, downloadRel, null)
                }
                applied = res.first
                msg = res.second
            } else {
                RealTimeLogger.w(TAG, "No download_url or download_rel provided in notification")
            }

            if (applied) {
                // send ACK (best-effort)
                try {
                    val latencyMs = 0L // we could measure apply latency; placeholder 0
                    sendAck(context, baseUrl, authToken, round, latencyMs, client)
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "Failed to send ACK for notif=$notifId: ${e.message}")
                }
            } else {
                RealTimeLogger.w(TAG, "Model not applied for notif=$notifId: $msg")
            }
        } catch (e: Exception) {
            RealTimeLogger.e(TAG, "handleWsNotification failed: ${e.message}", e)
        }
    }

    private fun notifKey(id: String) = "notif_" + id

    private fun randomizeDelay(baseMs: Long): Long {
        if (baseMs <= 0) return 0L
        val factor = 0.8 + Random.nextDouble() * 0.4 // 0.8 - 1.2
        return (baseMs * factor).toLong()
    }

    // ---------- Log upload with retry ----------

    /**
     * Upload multiple files as multipart/form-data to /api/logs/upload and respect 413/429 Retry-After.
     * Returns Pair(success, message)
     */
    suspend fun uploadLogsWithRetry(
        context: Context,
        baseUrl: String,
        authToken: String?,
        files: List<File>,
        client: OkHttpClient = OkHttpClient()
    ): Pair<Boolean, String> {
        return try {
            val requestBuilderSupplier: () -> Request = {
                val multipart = MultipartBody.Builder().setType(MultipartBody.FORM)
                files.forEachIndexed { idx, f ->
                    val media = guessMediaType(f.name)?.toMediaTypeOrNull() ?: "application/octet-stream".toMediaTypeOrNull()
                    multipart.addFormDataPart("file$idx", f.name, f.asRequestBody(media))
                }
                val body = multipart.build()
                val reqB = Request.Builder()
                    .url(baseUrl.trimEnd('/') + "/api/logs/upload")
                    .post(body)
                if (!authToken.isNullOrBlank()) reqB.addHeader("Authorization", "Bearer $authToken")
                reqB.build()
            }

            val resp = retryHttpWithBackoff {
                client.newCall(requestBuilderSupplier()).execute()
            }

            resp.use { r ->
                val text = r.body?.string() ?: ""
                if (r.isSuccessful) return Pair(true, text)
                return Pair(false, "HTTP ${r.code}: $text")
            }
        } catch (e: Exception) {
            RealTimeLogger.e(TAG, "uploadLogsWithRetry failed: ${e.message}", e)
            Pair(false, e.message ?: "error")
        }
    }

    private fun guessMediaType(name: String): String? {
        val lower = name.lowercase()
        return when {
            lower.endsWith(".txt") || lower.endsWith(".log") -> "text/plain"
            lower.endsWith(".json") -> "application/json"
            lower.endsWith(".pt") -> "application/octet-stream"
            lower.endsWith(".bin") -> "application/octet-stream"
            else -> null
        }
    }

    // ---------- ACK sender ----------

    /**
     * Send a simple ACK to /api/device/ack. round may be null.
     */
    suspend fun sendAck(
        context: Context,
        baseUrl: String,
        authToken: String?,
        round: Int?,
        latencyMs: Long,
        client: OkHttpClient = OkHttpClient()
    ): Boolean {
        return try {
            val json = if (round != null) {
                "{\"round\":$round,\"latency_ms\":$latencyMs,\"status\":\"applied\"}"
            } else {
                "{\"latency_ms\":$latencyMs,\"status\":\"applied\"}"
            }
            val body = json.toRequestBody("application/json".toMediaTypeOrNull())
            val reqB = Request.Builder()
                .url(baseUrl.trimEnd('/') + "/api/device/ack")
                .post(body)
            if (!authToken.isNullOrBlank()) reqB.addHeader("Authorization", "Bearer $authToken")
            val req = reqB.build()
            val resp = withContext(Dispatchers.IO) { client.newCall(req).execute() }
            resp.use {
                if (it.isSuccessful) return true
                RealTimeLogger.w(TAG, "ACK failed: ${it.code} ${it.body?.string()}")
                return false
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "sendAck failed: ${e.message}")
            return false
        }
    }
}
