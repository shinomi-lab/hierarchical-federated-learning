package com.example.hfl_experiment.training.data

/** 特徴量とラベルをまとめたシンプルなデータクラス */
data class DataSample(
    val features: FloatArray,
    val label: Int
)
