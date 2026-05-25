package com.example.hfl_experiment.network.response

import com.example.hfl_experiment.model.AppConfig

/**
 * /get_model_and_app エンドポイントからのレスポンスモデル
 */
data class GetModelAndConfigResponse(
    val model_b64: String,
    val config: List<AppConfig>
)
