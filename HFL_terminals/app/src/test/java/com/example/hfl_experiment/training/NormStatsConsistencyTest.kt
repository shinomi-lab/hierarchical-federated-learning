package com.example.hfl_experiment.training

import com.example.hfl_experiment.training.core.Golden
import com.example.hfl_experiment.training.data.CsvLoader
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class NormStatsConsistencyTest {
    @Test fun csvLoaderNormalization_matchesServerNormStats() {
        val n = Golden.json("golden/norm_stats.json")
        val csv = File("src/main/assets/training_data.csv")
        val raw = csv.useLines { ls -> ls.first { it.isNotBlank() && !it.startsWith("#") } }.split(',').map { it.trim().toFloat() }
        val first = CsvLoader.loadFlexible(csv, 2, 4).first()   // 先頭行の要求TP・要求RTT
        assertEquals(((raw[0] - n["tp_mean"].asFloat) / n["tp_std"].asFloat), first.features[0], 1e-4f)
        assertEquals(((raw[1] - n["rtt_mean"].asFloat) / n["rtt_std"].asFloat), first.features[1], 1e-4f)
    }
}
