// com/example/hfl_experiment/network/api/ApiService.kt
package com.example.hfl_experiment.network.api

import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.http.*
import retrofit2.Response
import com.google.gson.JsonElement

import okhttp3.ResponseBody


/** /send_to_device レスポンス（新旧どちらでも受けられる形） */
data class SendToDeviceDto(
    // 新形式
    val paths: Paths? = null,
    val links: Links? = null,
    // 旧形式（後方互換）
    val model: FileEntry? = null,
    val config: ConfigEntry? = null,
    val data: FileEntry? = null,
    // 追加: optional meta block that some edges return (checksum / expected size)
    val meta: Meta? = null,
) {
    data class Paths(
        val model_rel: String? = null,
        val config_rel: String? = null,
        val data_rel: String? = null,
    )
    data class Links(
        val model: String? = null,
        val config: String? = null,
        val data: String? = null,
    )
    data class FileEntry(
        val path: String? = null,
        val media_type: String? = null,
        val status_code: Int? = null
    )
    data class ConfigEntry(
        val body: JsonElement? = null,
        val status_code: Int? = null
    )
    // Meta block added to represent server-provided checksum/size info
    data class Meta(
        val data: MetaFile? = null,
        val model: MetaFile? = null
    ) {
        data class MetaFile(
            val content_sha256: String? = null,
            val expected_size_bytes: Long? = null,
            val media_type: String? = null
        )
    }
}

// com/example/hfl_experiment/network/api/ApiService.kt


data class MetaResp(
    val round: Int,
    val model_id: String,
    val base_hash: String,
    val preferred_dtype: String? = null
)

data class UploadAck(
    val status: String? = null,
    val terminal_id: String? = null,
    val n_samples: Int? = null,
    val sha256: String? = null
)
data class UploadResp(
    val status: String,          // "ok" など
    val sha256: String? = null,  // サーバ側で計算した受領ファイルのハッシュ
    val message: String? = null, // 任意の説明
    val round: Int? = null,      // 任意: 次ラウンドなど
    // 新規フィールド: サーバが明示的に ack を返す場合に備える
    val ack: Boolean? = null,
    val ack_timestamp: String? = null,
    val detail: String? = null
)



interface ApiService {
    @GET("api/v1/meta")
    suspend fun fetchMeta(): MetaResp

    @GET("send_to_device")
    suspend fun fetchSendToDevice(): SendToDeviceDto   // ★ここが必要

    @Multipart
    @POST("receive_terminal_weights/{terminal_id}")
    suspend fun uploadWeights(
        @Path("terminal_id") terminalId: String,
        @PartMap parts: Map<String, @JvmSuppressWildcards RequestBody>,
        @Part weights: MultipartBody.Part
    ): UploadResp

    // 新規: ラウンドリセット用エンドポイント
    @POST("utils/reset_round")
    suspend fun resetRound(): Response<Void>
}
