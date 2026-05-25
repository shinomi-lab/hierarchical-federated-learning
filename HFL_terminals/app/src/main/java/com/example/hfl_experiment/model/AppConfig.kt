package com.example.hfl_experiment.model

import com.google.gson.annotations.SerializedName // Gsonライブラリを使用する場合

// Gsonを使用する場合、build.gradleに implementation 'com.google.code.gson:gson:2.9.0' などを追加

data class AppConfig(
    @SerializedName("appType") val appType: String,
    @SerializedName("indicator") val indicator: String,
    @SerializedName("needRTT") val needRTT: Int,
    @SerializedName("needTP") val needTP: Int,
    @SerializedName("incRTT") val incRTT: Double, // JSONではFloatだがKotlinではDoubleが一般的
    @SerializedName("incTP") val incTP: Double
)

