package com.example.hfl_experiment.training

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class TrainingMetricsStoreTest {
    @get:Rule val tmp = TemporaryFolder()
    private lateinit var dir: File

    @Before fun setUp() { dir = tmp.newFolder(); TrainingMetricsStore.initDir(dir) }

    private fun row(id: String, vararg kv: Pair<String, Any>) =
        JSONObject().put("data_id", id).put("terminal_id", "t1").apply { kv.forEach { put(it.first, it.second) } }

    @Test fun recordRow_assignsDataId_whenMissing() {
        val id = TrainingMetricsStore.recordRow(JSONObject().put("terminal_id", "t1"))
        assertTrue(id.isNotBlank())
        assertEquals(id, TrainingMetricsStore.pending().single().getString("data_id"))
    }

    @Test fun markSent_removesOnlyThatRow_andKeepsLedger() {
        TrainingMetricsStore.recordRow(row("a")); TrainingMetricsStore.recordRow(row("b"))
        TrainingMetricsStore.markSent("a")
        assertEquals(listOf("b"), TrainingMetricsStore.pending().map { it.getString("data_id") })
        assertEquals(setOf("a", "b"), TrainingMetricsStore.latestRows().keys)
    }

    @Test fun rowAppendedWhileSending_survivesMarkSent() {
        TrainingMetricsStore.recordRow(row("a"))
        val snapshot = TrainingMetricsStore.pending()          // Worker が読んだ時点
        TrainingMetricsStore.recordRow(row("b"))               // 送信中に追記
        snapshot.forEach { TrainingMetricsStore.markSent(it.getString("data_id")) }
        assertEquals(listOf("b"), TrainingMetricsStore.pending().map { it.getString("data_id") })
    }

    @Test fun sameDataId_replacesInOutbox_andLastWinsInLedger() {
        TrainingMetricsStore.recordRow(row("a", "accuracy" to 0.1))
        TrainingMetricsStore.recordRow(row("a", "accuracy" to 0.1, "upload_success" to true))
        assertEquals(1, TrainingMetricsStore.pending().size)
        assertTrue(TrainingMetricsStore.pending().single().getBoolean("upload_success"))
        assertTrue(TrainingMetricsStore.latestRows().getValue("a").getBoolean("upload_success"))
        assertEquals(2, File(dir, "metrics_ledger.jsonl").readLines().count { it.isNotBlank() })
    }

    @Test fun lastCycleRow_picksMaxCycleOfRun() {
        TrainingMetricsStore.recordRow(row("a", "run_id" to "r1", "cycle_number" to 4))
        TrainingMetricsStore.recordRow(row("b", "run_id" to "r1", "cycle_number" to 5))
        TrainingMetricsStore.recordRow(row("c", "run_id" to "r2", "cycle_number" to 9))
        assertEquals("b", TrainingMetricsStore.lastCycleRow("r1")!!.getString("data_id"))
        assertNull(TrainingMetricsStore.lastCycleRow("none"))
    }

    @Test fun legacyFile_isMigratedToLedgerAndOutbox() {
        val legacyDir = tmp.newFolder()
        File(legacyDir, "training_metrics.jsonl").writeText(row("old").toString() + "\n")
        TrainingMetricsStore.initDir(legacyDir)
        assertEquals(listOf("old"), TrainingMetricsStore.pending().map { it.getString("data_id") })
        assertFalse(File(legacyDir, "training_metrics.jsonl").exists())
        assertTrue(TrainingMetricsStore.listAll()!!.contains("\"old\""))
    }

    // --- SP3d: After は最終サイクルの行（同じ data_id）に補完される ---
    @Test fun afterCompletion_isRecordedOnLastCycleRow() {
        TrainingMetricsStore.recordRow(row("a", "run_id" to "r1", "cycle_number" to 4))
        TrainingMetricsStore.recordRow(row("b", "run_id" to "r1", "cycle_number" to 5))
        val last = TrainingMetricsStore.lastCycleRow("r1")!!
        TrainingMetricsStore.recordRow(completeAfterMetric(last, summarizeAfterSamples(listOf(0.5, 0.6, 0.7, 0.6, 0.6)), 10L))
        val ledger = TrainingMetricsStore.listAll()!!
        assertTrue(ledger.contains("\"satisfaction_after_samples\""))
        val after = TrainingMetricsStore.lastCycleRow("r1")!!
        assertEquals("b", after.getString("data_id"))
        assertEquals(5, after.getInt("satisfaction_after_n"))
        assertEquals("b", TrainingMetricsStore.pending().last().getString("data_id"))
    }

    // --- SP3d 3.2: ベースラインの要約行が After の補完先になる（同じ run_id の実験イベント行は選ばない） ---
    @Test fun lastCycleRow_ignoresExperimentEvents_andFindsBaselineSessionRow() {
        TrainingMetricsStore.recordRow(row("ev", "run_id" to "r2", "kind" to "experiment_event", "type" to "hfl_mode_mismatch"))
        assertNull(TrainingMetricsStore.lastCycleRow("r2"))
        TrainingMetricsStore.recordRow(row("bs", "run_id" to "r2", "row_kind" to "baseline_session", "baseline_mode" to "fixed"))
        TrainingMetricsStore.recordRow(row("ev2", "run_id" to "r2", "kind" to "experiment_event", "type" to "x"))
        val target = TrainingMetricsStore.lastCycleRow("r2")!!
        assertEquals("bs", target.getString("data_id"))
        TrainingMetricsStore.recordRow(completeAfterMetric(target, summarizeAfterSamples(listOf(0.5, 0.6, 0.7, 0.6, 0.6)), 10L))
        val after = TrainingMetricsStore.lastCycleRow("r2")!!
        assertEquals("bs", after.getString("data_id"))
        assertEquals(5, after.getInt("satisfaction_after_n"))
        assertEquals("baseline_session", after.getString("row_kind"))
    }
}

