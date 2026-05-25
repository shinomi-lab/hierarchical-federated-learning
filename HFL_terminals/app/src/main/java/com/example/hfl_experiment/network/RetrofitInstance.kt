// com/example/hfl_experiment/network/RetrofitInstance.kt
package com.example.hfl_experiment.network

import android.content.Context
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import com.example.hfl_experiment.network.api.ApiService
import com.example.hfl_experiment.AppConfig
import com.example.hfl_experiment.BuildConfig
import com.example.hfl_experiment.experiment.ExperimentContext
import okhttp3.Interceptor

object RetrofitInstance {
    // logging client shared — BODY level only in debug builds to avoid leaking weights in production logs
    private val logging = HttpLoggingInterceptor().apply {
        level = if (BuildConfig.DEBUG) HttpLoggingInterceptor.Level.BODY
                else HttpLoggingInterceptor.Level.NONE
    }

    // 実験追跡ヘッダを全リクエストに自動付与するインターセプター
    private val experimentHeaderInterceptor = Interceptor { chain ->
        val req = chain.request().newBuilder()
            .addHeader("X-Run-Id", ExperimentContext.runId.ifEmpty { "none" })
            .addHeader("X-Terminal-Id", ExperimentContext.terminalId.ifEmpty { "unknown" })
            .addHeader("X-Round-Id", ExperimentContext.currentRound.toString())
            .build()
        chain.proceed(req)
    }

    private val client = OkHttpClient.Builder()
        .addInterceptor(experimentHeaderInterceptor)
        .addInterceptor(logging)
        .build()

    // Create an ApiService that uses AppConfig.getEdgeBaseUrl(context) so runtime prefs can override
    fun createApi(context: Context): ApiService {
        val baseUrl = AppConfig.getEdgeBaseUrl(context).trimEnd('/') + "/"
        val retrofit = Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
        return retrofit.create(ApiService::class.java)
    }
}
