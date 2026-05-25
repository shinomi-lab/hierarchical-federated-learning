package com.example.hfl_experiment.model // パッケージ名は適宜変更してください

import com.google.gson.annotations.SerializedName

/**
 * サーバーから取得する各種ダウンロードパス情報を格納するデータクラス。
 */
data class DownloadPaths(
    @SerializedName("model_path")  val modelPath: String?,
    @SerializedName("config_path") val configPath: String?,
    @SerializedName("data_path")   val dataPath: String?
)
