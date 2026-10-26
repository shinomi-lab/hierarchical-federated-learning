package com.example.hfl_experiment.training

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class CycleMetricTest {
    @Test fun summarizeEpochs_lastAndMean() {
        val s = summarizeEpochs(listOf(0.5, null, 0.7), listOf(1.0, 0.8, 0.6))
        assertEquals(0.7, s.lastAcc!!, 1e-12); assertEquals(0.6, s.lastLoss!!, 1e-12)
        assertEquals(0.6, s.meanAcc!!, 1e-12); assertEquals(0.8, s.meanLoss!!, 1e-12)
        assertEquals(listOf(0.5, 0.7), s.accs)
    }

    @Test fun summarizeEpochs_empty() {
        val s = summarizeEpochs(emptyList(), emptyList())
        assertNull(s.lastAcc); assertNull(s.meanAcc); assertTrue(s.accs.isEmpty())
    }

    @Test fun trainingMetric_omitsRound_whenUnknown_andWritesNewKeys() {
        val jo = TrainingMetricsStore.TrainingMetric(
            terminalId = "t1", timestamp = 1L, totalLocalMs = 2L, epochDurations = listOf(1L),
            inputSize = 6, hiddenSize = 8, outputSize = 2, round = null, accuracy = 0.7,
            valAccuracyMean = 0.6, epochValAccuracy = listOf(0.5, 0.7), virtualRouterId = "HFL_A24"
        ).toJson()
        assertFalse(jo.has("round"))
        assertEquals(0.6, jo.getDouble("val_accuracy_mean"), 1e-12)
        assertEquals(2, jo.getJSONArray("epoch_val_accuracy").length())
        assertEquals("HFL_A24", jo.getString("virtual_router_id"))
    }

    @Test fun completeCycleMetric_keepsDataId_andFillsPostUploadValues() {
        val row = JSONObject().put("data_id", "d1").put("accuracy", 0.7)
        val done = completeCycleMetric(row, UploadOutcome(true, 420L, "WIFI"), 0.8, 12.5, 30.0)
        assertEquals("d1", done.getString("data_id"))
        assertEquals(0.7, done.getDouble("accuracy"), 1e-12)
        assertTrue(done.getBoolean("upload_success")); assertEquals(420L, done.getLong("upload_duration_ms"))
        assertEquals("WIFI", done.getString("network_type_at_upload"))
        assertEquals(0.8, done.getDouble("satisfaction_cycle"), 1e-12)
        assertEquals(12.5, done.getDouble("tp_measured_mbps"), 1e-12); assertEquals(30.0, done.getDouble("rtt_measured_ms"), 1e-12)
        assertFalse(row.has("upload_success"))   // 元の行は変更しない
    }

    @Test fun completeCycleMetric_nullArgs_leaveFieldsUntouched() {
        val row = JSONObject().put("data_id", "d1").put("tp_measured_mbps", 3.0)
        val done = completeCycleMetric(row, null, null, null, null)
        assertFalse(done.has("upload_success")); assertEquals(3.0, done.getDouble("tp_measured_mbps"), 1e-12)
    }

    @Test fun embedMetric_putsRow_andDropsEpochArraysWhenTooLarge() {
        val row = JSONObject().put("data_id", "d1").put("epoch_val_accuracy", org.json.JSONArray(List(2000) { 0.123456789 }))
        val small = embedMetricInClientMeta(JSONObject().put("a", 1), JSONObject().put("data_id", "d0"))
        assertEquals("d0", small.getJSONObject("metric").getString("data_id"))
        val big = embedMetricInClientMeta(JSONObject().put("a", 1), row)
        assertEquals("d1", big.getJSONObject("metric").getString("data_id"))
        assertFalse(big.getJSONObject("metric").has("epoch_val_accuracy"))
        assertTrue(big.toString().toByteArray().size <= 8192)
    }

    @Test fun resolveAppType_blankIsUnknown() {
        assertEquals("unknown", resolveAppType(null)); assertEquals("unknown", resolveAppType(" "))
        assertEquals("動画", resolveAppType("動画"))
    }

    // --- SP3d: サイクル数の記録 ---
    @Test fun withSessionSettings_addsNumCycles() {
        val row = JSONObject().put("data_id", "d1")
        val r = withSessionSettings(row, 20, "server")
        assertEquals(20, r.getInt("num_cycles")); assertEquals("server", r.getString("num_cycles_source"))
        assertEquals("d1", r.getString("data_id"))
        assertFalse(row.has("num_cycles"))
    }

    @Test fun completedMessage_usesSessionCycles() {
        assertTrue(cyclesCompletedMessage(20).startsWith("20サイクル学習完了"))
        assertTrue(cyclesCompletedMessage(3).startsWith("3サイクル学習完了"))
    }

    // --- SP3d: After の複数回計測 ---
    @Test fun afterConstants() {
        assertEquals(5, AFTER_SAMPLES); assertEquals(3000L, AFTER_INTERVAL_MS)
    }

    @Test fun summarizeAfterSamples_meanStdN_ignoringNull() {
        val s = summarizeAfterSamples(listOf(0.5, null, 0.7))
        assertEquals(0.6, s.mean!!, 1e-12); assertEquals(2, s.n)
        assertEquals(0.1414213562, s.std!!, 1e-9)                // 不偏標準偏差（n−1）
        assertEquals(listOf(0.5, null, 0.7), s.samples)
    }

    @Test fun summarizeAfterSamples_specScenario() {
        val s = summarizeAfterSamples(listOf(0.5, 0.6, null, 0.7, 0.6))
        assertEquals(0.6, s.mean!!, 1e-12); assertEquals(4, s.n); assertEquals(5, s.samples.size)
    }

    @Test fun summarizeAfterSamples_edgeCases() {
        val none = summarizeAfterSamples(listOf(null, null))
        assertNull(none.mean); assertNull(none.std); assertEquals(0, none.n)
        val one = summarizeAfterSamples(listOf(0.4))
        assertEquals(0.4, one.mean!!, 1e-12); assertNull(one.std); assertEquals(1, one.n)
        val nan = summarizeAfterSamples(listOf(Double.NaN, 0.4))    // NaN は失敗扱い
        assertEquals(1, nan.n)
    }

    @Test fun completeAfterMetric_keepsDataId_andAddsSamples() {
        val row = JSONObject().put("data_id", "d1").put("satisfaction_cycle", 0.8)
        val r = completeAfterMetric(row, summarizeAfterSamples(listOf(0.5, null, 0.7)), 99L)
        assertEquals("d1", r.getString("data_id")); assertEquals(0.6, r.getDouble("satisfaction_after"), 1e-12)
        assertEquals(3, r.getJSONArray("satisfaction_after_samples").length())
        assertTrue(r.getJSONArray("satisfaction_after_samples").isNull(1))
        assertEquals(0.1414213562, r.getDouble("satisfaction_after_std"), 1e-9)
        assertEquals(2, r.getInt("satisfaction_after_n")); assertEquals(99L, r.getLong("timestamp_ms"))
        assertEquals(0.8, r.getDouble("satisfaction_cycle"), 1e-12)
        assertFalse(row.has("satisfaction_after"))
    }

    @Test fun completeAfterMetric_singleSample_hasNullStd() {
        val r = completeAfterMetric(JSONObject().put("data_id", "d1"), summarizeAfterSamples(listOf(0.4)), 1L)
        assertTrue(r.isNull("satisfaction_after_std")); assertEquals(1, r.getInt("satisfaction_after_n"))
    }

    @Test fun putAfterSummary_writesNullsWhenNotMeasured() {
        val jo = putAfterSummary(JSONObject(), null)
        assertTrue(jo.isNull("satisfaction_after_samples")); assertTrue(jo.isNull("satisfaction_after_std"))
        assertTrue(jo.isNull("satisfaction_after_n"))
        val jo2 = putAfterSummary(JSONObject(), summarizeAfterSamples(listOf(0.5, 0.7)))
        assertEquals(2, jo2.getJSONArray("satisfaction_after_samples").length()); assertEquals(2, jo2.getInt("satisfaction_after_n"))
    }

    // --- SP3d 3.2: ベースラインのセッション要約行 ---
    private fun baselineInfo() = BaselineSessionInfo(
        runId = "r1", terminalId = "terminal-01", experimentGroup = "g", baselineMode = "fixed", baselineModeSource = "server",
        hflMode = "sync", hflModeSource = "app", numCycles = 3, numCyclesSource = "server", plannedMeasurements = 15,
        appType = "動画", appIndex = 1, virtualRouterId = "HFL_A24", timestampMs = 42L,
    )

    @Test fun baselineSessionRow_hasSessionFields_andNoCycleNumber() {
        val r = buildBaselineSessionRow(baselineInfo(), 0.4, listOf(0.5, null, 0.7))
        assertEquals("baseline_session", r.getString("row_kind"))
        assertTrue(r.getString("data_id").isNotBlank())
        assertEquals("r1", r.getString("run_id")); assertEquals("terminal-01", r.getString("terminal_id"))
        assertEquals("fixed", r.getString("baseline_mode")); assertEquals("server", r.getString("baseline_mode_source"))
        assertEquals("sync", r.getString("hfl_mode")); assertEquals(3, r.getInt("num_cycles"))
        assertEquals("server", r.getString("num_cycles_source")); assertEquals(15, r.getInt("baseline_measurements"))
        assertEquals(0.4, r.getDouble("satisfaction_before"), 1e-12)
        assertEquals(0.6, r.getDouble("satisfaction_baseline_mean"), 1e-12); assertEquals(2, r.getInt("satisfaction_baseline_n"))
        assertEquals(3, r.getJSONArray("satisfaction_baseline_samples").length())
        assertTrue(r.getJSONArray("satisfaction_baseline_samples").isNull(1))
        assertEquals("動画", r.getString("app_type")); assertEquals(42L, r.getLong("timestamp_ms"))
        assertFalse(r.has("cycle_number")); assertFalse(r.has("accuracy")); assertFalse(r.has("satisfaction_after"))
    }

    @Test fun baselineSessionRow_nullBefore_andAllFailed() {
        val r = buildBaselineSessionRow(baselineInfo(), null, listOf(null, null))
        assertTrue(r.isNull("satisfaction_before")); assertTrue(r.isNull("satisfaction_baseline_mean"))
        assertEquals(0, r.getInt("satisfaction_baseline_n"))
    }
}

