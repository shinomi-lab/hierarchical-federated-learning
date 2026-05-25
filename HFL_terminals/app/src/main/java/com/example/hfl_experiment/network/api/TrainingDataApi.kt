package com.example.hfl_experiment.network.api

import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part

/**
 * Minimal Retrofit interface for sending training metrics and sample uploads from Android client.
 */
interface TrainingDataApi {
    @POST("/api/training-data")
    suspend fun sendMetrics(@Body body: TrainingMetrics): Response<MetricsResponse>

    @Multipart
    @POST("/api/training-data/upload-sample")
    suspend fun uploadSample(
        @Part file: MultipartBody.Part,
        @Part("device_id") deviceId: RequestBody,
        @Part("round_number") roundNumber: RequestBody,
        @Part("label") label: RequestBody?,
        @Part("sample_type") sampleType: RequestBody?,
        @Part("timestamp") timestamp: RequestBody?
    ): Response<UploadResponse>
}

