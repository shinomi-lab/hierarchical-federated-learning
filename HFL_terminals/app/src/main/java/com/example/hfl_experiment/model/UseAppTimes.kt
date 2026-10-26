package com.example.hfl_experiment.model

import com.google.gson.annotations.SerializedName

data class UseAppTimes(
    @SerializedName("minTime") val minTime: Int,
    @SerializedName("maxTime") val maxTime: Int
)
