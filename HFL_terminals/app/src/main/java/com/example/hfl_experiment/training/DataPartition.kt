package com.example.hfl_experiment.training

/**
 * fix-audit-20261015 E8: 実機の端末が学習する教師データの部分。
 * 以前は terminal_index を書くコードが無く、4 台とも先頭の部分（0 番）を学習していた。
 */

/**
 * 端末の番号（0 始まり）と出所。設定（hfl_prefs の terminal_index）が範囲内ならそれ（"pref"）、
 * 無ければ端末 ID の末尾の数字 − 1（"terminal_id"、例 terminal-03 → 2、device-001 → 0）、どちらも駄目なら 0（"default"）。
 */
fun resolveTerminalIndex(prefIndex: Int?, terminalId: String, count: Int): Pair<Int, String> {
    if (prefIndex != null && prefIndex in 0 until count) return prefIndex to "pref"
    val digits = Regex("(\\d+)$").find(terminalId.trim())?.groupValues?.get(1)?.toIntOrNull()
    if (digits != null && (digits - 1) in 0 until count) return (digits - 1) to "terminal_id"
    return 0 to "default"
}

/** データを count 個に分けた index 番目（大きさ ceil(n/count)、以前の chunked と同じ分け方）。範囲外なら先頭の部分。 */
fun <T> partitionRows(rows: List<T>, index: Int, count: Int): List<T> {
    if (rows.isEmpty()) return rows
    val chunkSize = (rows.size + count - 1) / count.coerceAtLeast(1)
    val parts = rows.chunked(chunkSize)
    return parts.getOrNull(index) ?: parts.first()
}

data class DataPartitionInfo(val index: Int, val count: Int, val rows: Int, val source: String)

/** メトリクスの行に、使ったデータの部分を入れる。 */
fun putDataPartition(row: org.json.JSONObject, p: DataPartitionInfo?): org.json.JSONObject {
    if (p == null) return row
    return row.put("data_partition_index", p.index).put("data_partition_count", p.count)
        .put("data_partition_rows", p.rows).put("data_partition_source", p.source)
}
