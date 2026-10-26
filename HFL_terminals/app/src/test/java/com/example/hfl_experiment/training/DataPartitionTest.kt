package com.example.hfl_experiment.training

import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.robolectric.annotation.Config

/** fix-audit-20261015 E8: 実機の端末は教師データの別の部分を学習する。番号は 設定 → 端末 ID の末尾の数字 − 1 → 0。 */
class DataPartitionTest {
    @Test fun prefWins() = assertEquals(1 to "pref", resolveTerminalIndex(1, "terminal-03", 4))
    @Test fun fromTerminalId() = assertEquals(2 to "terminal_id", resolveTerminalIndex(null, "terminal-03", 4))
    @Test fun fromDeviceStyleId() = assertEquals(0 to "terminal_id", resolveTerminalIndex(null, "device-001", 4))
    @Test fun noDigitsIsDefault() = assertEquals(0 to "default", resolveTerminalIndex(null, "pixel", 4))
    @Test fun outOfRangeIsDefault() = assertEquals(0 to "default", resolveTerminalIndex(null, "terminal-05", 4))
    @Test fun invalidPrefFallsBackToId() = assertEquals(3 to "terminal_id", resolveTerminalIndex(7, "terminal-04", 4))

    @Test fun fourTerminalsCoverAllRowsWithoutOverlap() {
        val rows = (0 until 8752).toList()
        val parts = (1..4).map { partitionRows(rows, resolveTerminalIndex(null, "terminal-0$it", 4).first, 4) }
        assertEquals(listOf(2188, 2188, 2188, 2188), parts.map { it.size })
        assertEquals(rows, parts.flatten())
    }
}

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class DataPartitionMetricsTest {
    @Test fun metricsRowHasPartition() {
        val row = putDataPartition(org.json.JSONObject(), DataPartitionInfo(2, 4, 2188, "terminal_id"))
        assertEquals(2, row.getInt("data_partition_index"))
        assertEquals(4, row.getInt("data_partition_count"))
        assertEquals(2188, row.getInt("data_partition_rows"))
        assertEquals("terminal_id", row.getString("data_partition_source"))
    }
}
