package com.example.hfl_experiment.training.core

import com.example.hfl_experiment.training.data.DataSample

/**
 * データセットの一部を表すクラス
 * Pythonの DatasetSplit に相当
 */
class DatasetSplit(
    private val fullDataset: List<DataSample>,
    private val indices: List<Int>
) {
    val size: Int
        get() = indices.size

    fun getItem(index: Int): DataSample {
        if (index !in indices.indices) {
            throw IndexOutOfBoundsException("Index $index is out of bounds for DatasetSplit of size $size")
        }
        return fullDataset[indices[index]]
    }

    fun generateBatches(batchSize: Int, shuffle: Boolean = true): List<List<DataSample>> {
        val current = if (shuffle) indices.shuffled() else indices
        return current.chunked(batchSize).map { batchIndices ->
            batchIndices.map { fullDataset[it] }
        }
    }
}
