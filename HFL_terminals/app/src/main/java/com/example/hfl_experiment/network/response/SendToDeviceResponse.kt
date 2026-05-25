package com.example.hfl_experiment.network.response

/**
 * /send_to_device エンドポイントからのパス情報レスポンス
 */
data class SendToDeviceResponse(
    val model_path: String,
    val config_path: String
)
