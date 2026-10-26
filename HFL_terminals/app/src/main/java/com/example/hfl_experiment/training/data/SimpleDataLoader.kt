package com.example.hfl_experiment.training.data

import com.example.hfl_experiment.util.logging.RealTimeLogger

object CsvLoader {
    /** 例: 各行 "10,0,200,1" → 先頭3つが特徴、末尾がラベル */
    fun load(path: java.io.File, featureCount: Int): List<DataSample> =
        path.bufferedReader().useLines { lines ->
            var parsed = lines.filter { it.isNotBlank() }.map { line ->
                val t = line.split(',')
                require(t.size >= featureCount + 1) { "columns < featureCount+1: $line" }
                val feats = FloatArray(featureCount) { i -> t[i].toFloat() }
                val label = t[featureCount].toInt()
                DataSample(feats, label)
            }.toMutableList()

            // Detect if features look like pixel values (0..255) and normalize if so
            try {
                val allMax = parsed.flatMap { it.features.asList() }.maxOrNull() ?: 0f
                val allMin = parsed.flatMap { it.features.asList() }.minOrNull() ?: 0f
                if (allMax > 1f && allMax <= 255f && allMin >= 0f) {
                    RealTimeLogger.i("CsvLoader", "Normalizing features by 255.0 because detected max=$allMax")
                    parsed.forEach { s -> for (i in s.features.indices) s.features[i] = s.features[i] / 255.0f }
                }
            } catch (_: Exception) {}

            // Detect 1-indexed labels (if min label >=1 and max label looks like categories count) and shift
            try {
                val minLabel = parsed.minOfOrNull { it.label } ?: 0
                val maxLabel = parsed.maxOfOrNull { it.label } ?: 0
                if (minLabel >= 1) {
                    RealTimeLogger.i("CsvLoader", "Detected labels appear 1-indexed (min=$minLabel,max=$maxLabel) — shifting labels to 0-indexed")
                    // DataSample.label is immutable; create new DataSample objects with shifted label
                    parsed = parsed.map { s -> DataSample(s.features, (s.label - 1).coerceAtLeast(0)) }.toMutableList()
                }
            } catch (_: Exception) {}

            parsed.toList()
        }

    /**
     * Flexible CSV loader.
     * Supports several formats (per-line):
     * - tp,rtt,<one-hot app...>,label
     * - tp,rtt,label                (compact: label is app index)
     * - tp,rtt,app_idx,label        (explicit app index + label)
     * - tp,rtt,app_idx              (compact: use app_idx as label)
     *
     * Parameters:
     * - tpRttFeatures: number of base numeric features at start (usually 2)
     * - appNumMax: number of app categories to expand into one-hot
     */
    fun loadFlexible(path: java.io.File, tpRttFeatures: Int, appNumMax: Int): List<DataSample> {
        // Read all lines and parse into intermediate records so we can compute means/stds for base features
        data class RawEntry(val base: FloatArray, var label: Int, var appIdx: Int = -1)

        val entries = mutableListOf<RawEntry>()
        path.bufferedReader().useLines { rawLines ->
            rawLines.map { it.trim() }
                .filter { it.isNotEmpty() && !it.startsWith("#") }
                .forEach { line ->
                    try {
                        val cols = line.split(',').map { it.trim() }
                        val n = cols.size
                        fun pf(s: String, idx: Int) = s.toFloatOrNull() ?: throw IllegalArgumentException("col[$idx] not float: '$s'")
                        fun pi(s: String, idx: Int) = s.toIntOrNull() ?: throw IllegalArgumentException("col[$idx] not int: '$s'")

                        when {
                            // already one-hot + label: tpRttFeatures + appNumMax + 1
                            n >= tpRttFeatures + appNumMax + 1 -> {
                                val base = FloatArray(tpRttFeatures) { i -> pf(cols[i], i) }
                                val label = pi(cols[tpRttFeatures + appNumMax], tpRttFeatures + appNumMax)
                                entries.add(RawEntry(base, label))
                            }
                            // tp,rtt,label
                            n == tpRttFeatures + 1 -> {
                                val base = FloatArray(tpRttFeatures) { i -> pf(cols[i], i) }
                                val label = pi(cols[tpRttFeatures], tpRttFeatures)
                                entries.add(RawEntry(base, label))
                            }
                            // tp,rtt,app_idx  OR tp,rtt,app_idx,label ambiguous -> treat last as label if present
                            n == tpRttFeatures + 2 -> {
                                val base = FloatArray(tpRttFeatures) { i -> pf(cols[i], i) }
                                val a = cols[tpRttFeatures].toIntOrNull()
                                val b = cols[tpRttFeatures + 1].toIntOrNull()
                                if (b != null) {
                                    // treat last as label
                                    entries.add(RawEntry(base, b, a ?: -1))
                                } else if (a != null) {
                                    entries.add(RawEntry(base, a))
                                } else {
                                    throw IllegalArgumentException("Unable to parse label/app idx for line: $line")
                                }
                            }
                            // tp,rtt,app_idx,label explicit
                            n == tpRttFeatures + 3 -> {
                                val base = FloatArray(tpRttFeatures) { i -> pf(cols[i], i) }
                                val label = pi(cols[tpRttFeatures + 1], tpRttFeatures + 1)
                                // label is explicit
                                entries.add(RawEntry(base, label))
                            }
                            // fallback: treat last as label
                            n >= tpRttFeatures + 1 -> {
                                val base = FloatArray(tpRttFeatures) { i -> pf(cols[i], i) }
                                val label = pi(cols.last(), n - 1)
                                entries.add(RawEntry(base, label))
                            }
                        }
                    } catch (e: Exception) {
                        try { RealTimeLogger.w("CsvLoader", "Skipping malformed CSV line: $line -> ${e.message}") } catch (_: Exception) {}
                    }
                }
        }

        if (entries.isEmpty()) return emptyList()

        // Compute mean/std for each base feature (tp, rtt, ...)
        val means = FloatArray(tpRttFeatures)
        val stds = FloatArray(tpRttFeatures)
        for (i in 0 until tpRttFeatures) {
            val colVals = entries.map { it.base[i] }
            val mean = colVals.average().toFloat()
            val variance = colVals.map { (it - mean) * (it - mean) }.average().toFloat()
            val std = kotlin.math.sqrt(variance.toDouble()).toFloat()
            means[i] = mean
            stds[i] = if (std <= 1e-8f) 1f else std
        }
        RealTimeLogger.i("CsvLoader", "Computed base means=${means.joinToString()} stds=${stds.joinToString()}")

        // Detect 1-indexed labels and shift if necessary
        val minLabel = entries.minOfOrNull { it.label } ?: 0
        val maxLabel = entries.maxOfOrNull { it.label } ?: 0
        if (minLabel >= 1) {
            RealTimeLogger.i("CsvLoader", "Detected labels 1-indexed (min=${minLabel},max=${maxLabel}) -> shifting to 0-indexed")
            entries.forEach { it.label = (it.label - 1).coerceAtLeast(0) }
        }

        // Determine actual app count from labels
        val actualAppNum = (entries.maxOfOrNull { it.label } ?: 0) + 1
        val finalAppNum = actualAppNum.coerceAtLeast(appNumMax)

        // Build final DataSample list with normalized base features and one-hot app features
        val out = entries.map { re ->
            val normBase = FloatArray(tpRttFeatures)
            for (i in 0 until tpRttFeatures) normBase[i] = (re.base[i] - means[i]) / (stds[i] + 1e-8f)
            val feat = FloatArray(tpRttFeatures + finalAppNum)
            for (i in 0 until tpRttFeatures) feat[i] = normBase[i]
            val lbl = re.label.coerceAtLeast(0)
            val oneHotIdx = if (re.appIdx >= 0) re.appIdx else lbl
            for (j in 0 until finalAppNum) feat[tpRttFeatures + j] = if (j == oneHotIdx) 1.0f else 0.0f
            DataSample(feat, lbl)
        }
        return out.toList()
    }
}


class SimpleDataLoader(
    private val data: List<DataSample>,
    private val batchSize: Int,
    private val shuffle: Boolean = true,
    private val seed: Long? = null
) : Iterable<List<DataSample>> {
    override fun iterator(): Iterator<List<DataSample>> {
        val list = if (shuffle) {
            if (seed == null) data.shuffled() else data.shuffled(kotlin.random.Random(seed))
        } else data
        val chunks = ArrayList<List<DataSample>>()
        var i = 0
        while (i < list.size) {
            val j = kotlin.math.min(i + batchSize, list.size)
            chunks.add(list.subList(i, j))
            i = j
        }
        return chunks.iterator()
    }
}
