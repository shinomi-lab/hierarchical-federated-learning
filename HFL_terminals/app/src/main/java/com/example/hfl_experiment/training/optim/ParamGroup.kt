package com.example.hfl_experiment.training.optim

// package com.example.hfl_experiment.training.optim

/** モデルパラメータをグループ化し、lr/正則化などを束ねる */
data class ParamGroup(
    /** このグループに属するパラメータ（参照） */
    val params: List<ParamRef>,
    /** 学習率 */
    var lr: Float,
    /** (beta1, beta2) */
    var betas: Pair<Float, Float> = 0.9f to 0.999f,
    /** 数値安定化 */
    var eps: Float = 1e-8f,
    /** AdamW 方式の decoupled weight decay（bias には通常 0 を推奨） */
    var weightDecay: Float = 0f,
    /** AMSGrad を使うか */
    var amsgrad: Boolean = false
)

/** パラメータ配列の一部（ビュー）を参照する軽量ハンドル */
data class ParamRef(
    val array: FloatArray,  // 実体
    val grad: FloatArray,   // 勾配（呼び出し側で埋める）
    val offset: Int = 0,    // array 内の開始位置
    val length: Int = array.size  // このパラメータの長さ
) {
    init {
        require(grad.size == length) { "grad length mismatch: ${grad.size} != $length" }
        require(offset >= 0 && offset + length <= array.size) { "ParamRef range out of bounds" }
    }
}
