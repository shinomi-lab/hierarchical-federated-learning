package com.example.hfl_experiment.experiment

import com.google.gson.JsonParser
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.InterruptedIOException
import java.util.concurrent.TimeUnit

/** 中央への 1 回の問い合わせの結果。失敗のとき chosenAp 以下は null。rttMs は送信から結果が分かるまで。 */
data class CentralOutcome(
    val status: String,
    val rttMs: Long,
    val chosenAp: Int? = null,
    val scores: List<Double>? = null,
    val modelSha256: String? = null,
    val serverMs: Double? = null,
    val httpCode: Int? = null,
    val url: String = "",
    val round: Int? = null,          // hfl_rec_infer の応答のみ: モデルのラウンド
    val isInitial: Boolean? = null,  // hfl_rec_infer の応答のみ: 初期モデル（集約前）か
)

/**
 * central モード（centralized-mode）で中央サーバへ「このアプリなら AP は？」と問い合わせるクライアント。
 * Android に依存しない（URL・タイムアウト・時計を注入できる）。エッジは経由しない。
 * 失敗しても例外にせず status で返す。再試行も、端末内モデルへの切り替えもしない（失敗率そのものが測定対象）。
 * 呼び出しはブロックするので、呼び出し側が IO スレッドで使う。
 */
class CentralClient(
    baseUrl: String,
    private val timeoutMs: Long = DEFAULT_TIMEOUT_MS,
    private val nanoTime: () -> Long = System::nanoTime,
    private val http: OkHttpClient = OkHttpClient(),
    private val path: String = PATH,   // 問い合わせ先。hfl_rec は HFL_REC_PATH
) {
    private val baseUrl: String = baseUrl.trim().trimEnd('/')
    val endpointUrl: String get() = "$baseUrl$path"

    fun query(terminalId: String, appType: String): CentralOutcome {
        if (baseUrl.isEmpty()) return CentralOutcome(STATUS_NOT_CONFIGURED, 0)
        val url = endpointUrl
        val body = com.google.gson.JsonObject().apply {
            addProperty("terminal_id", terminalId)
            addProperty("app_type", appType)
        }.toString().toRequestBody(JSON)
        val call = http.newBuilder()
            .callTimeout(timeoutMs, TimeUnit.MILLISECONDS)
            .build()
            .newCall(Request.Builder().url(url).post(body).build())
        val t0 = nanoTime()
        fun rtt() = (nanoTime() - t0) / 1_000_000L
        return try {
            call.execute().use { resp ->
                val text = resp.body.string()
                val code = resp.code
                when {
                    code in 500..599 -> CentralOutcome(STATUS_SERVER_ERROR, rtt(), httpCode = code, url = url)
                    code in 400..499 -> CentralOutcome(STATUS_BAD_REQUEST, rtt(), httpCode = code, url = url)
                    code != 200 -> CentralOutcome(STATUS_BAD_RESPONSE, rtt(), httpCode = code, url = url)
                    else -> parse(text, rtt(), code, url)
                }
            }
        } catch (_: InterruptedIOException) {   // SocketTimeoutException と call timeout を含む
            CentralOutcome(STATUS_TIMEOUT, rtt(), url = url)
        } catch (_: java.io.IOException) {
            CentralOutcome(STATUS_UNREACHABLE, rtt(), url = url)
        }
    }

    private fun parse(text: String, rttMs: Long, code: Int, url: String): CentralOutcome = try {
        val o = JsonParser.parseString(text).asJsonObject
        val ap = o["chosen_ap"].asInt
        val scores = o["scores"].asJsonArray.map { it.asDouble }
        val sha = o["model_sha256"].asString
        if (ap < 0 || scores.isEmpty() || sha.isBlank()) throw IllegalArgumentException("invalid fields")
        CentralOutcome(
            STATUS_OK, rttMs, chosenAp = ap, scores = scores, modelSha256 = sha,
            serverMs = o["server_ms"]?.takeIf { !it.isJsonNull }?.asDouble, httpCode = code, url = url,
            round = o["round"]?.takeIf { !it.isJsonNull }?.asInt,
            isInitial = o["is_initial"]?.takeIf { !it.isJsonNull }?.asBoolean,
        )
    } catch (_: Exception) {
        CentralOutcome(STATUS_BAD_RESPONSE, rttMs, httpCode = code, url = url)
    }

    companion object {
        const val PATH = "/api/v1/central_infer"
        const val HFL_REC_PATH = "/api/v1/hfl_rec_infer"
        const val DEFAULT_TIMEOUT_MS = 5000L
        const val STATUS_OK = "ok"
        const val STATUS_TIMEOUT = "timeout"
        const val STATUS_UNREACHABLE = "unreachable"
        const val STATUS_SERVER_ERROR = "server_error"
        const val STATUS_BAD_REQUEST = "bad_request"
        const val STATUS_BAD_RESPONSE = "bad_response"
        const val STATUS_NOT_CONFIGURED = "not_configured"
        private val JSON = "application/json; charset=utf-8".toMediaType()
    }
}
