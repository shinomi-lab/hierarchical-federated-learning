package com.example.hfl_experiment.network.api

import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.Response
import retrofit2.http.*

interface ClientLogsApi {
    @Multipart
    @POST("upload_client_logs/{terminalId}")
    suspend fun uploadClientLogs(
        @Path("terminalId") terminalId: String,
        @Part("meta") meta: RequestBody?,
        @Part files: List<MultipartBody.Part>,
        @Header("Authorization") auth: String
    ): Response<UploadResult>

    @GET("upload_client_logs/{terminalId}/{sha256}")
    suspend fun checkExists(
        @Path("terminalId") terminalId: String,
        @Path("sha256") sha256: String
    ): Response<ExistsResult>
}

data class UploadResult(val ack: Boolean, val status: String, val received: List<ReceivedFile>)

data class ReceivedFile(val name: String, val size: Long, val sha256: String)

data class ExistsResult(val exists: Boolean)

