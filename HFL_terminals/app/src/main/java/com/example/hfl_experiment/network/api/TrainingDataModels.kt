package com.example.hfl_experiment.network.api

data class TrainingMetrics(
    val roundNumber: Int,
    val terminalId: String?,
    val epoch: Int,
    val batchSize: Int,
    val learningRate: Double,
    val trainingLoss: Double,
    val validationLoss: Double,
    val trainingAccuracy: Double,
    val validationAccuracy: Double,
    val uploadSize: Int,
    val downloadSize: Int,
    val communicationTime: Int,
    val cpuUsage: Double,
    val memoryUsage: Double,
    val batteryUsage: Double,
    val datasetSize: Int,
    val timestamp: Long,
    val communicationErrors: Int = 0,
    val trainingErrors: Int = 0,
    val dataDistribution: Map<String, Int>? = null,
    val preprocessingTime: Int = 0,
)

data class MetricsResponse(val status: String?, val id: Long?, val ackTimestamp: Long?)

data class UploadResponse(val status: String?, val sha256: String?)

