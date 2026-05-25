package com.example.hfl_experiment.model

import com.google.gson.annotations.SerializedName

data class ServerDataResponse(
    @SerializedName("model")
    val modelDataString: String, // モデルファイルのデータ (latin1エンコードされた文字列)

    @SerializedName("sim")
    val simDataString: String    // シミュレーション情報のJSON文字列
)