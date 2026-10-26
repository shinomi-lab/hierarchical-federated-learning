package com.example.hfl_experiment.experiment

import com.example.hfl_experiment.training.BaselineSessionInfo
import com.example.hfl_experiment.training.buildBaselineSessionRow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** central セッションの判断と記録（centralized-mode 5.5・5.6b・5.7）。JVM 上で判断を固定する。 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class CentralSessionTest {
    private fun ok(rtt: Long, ap: Int, sha: String = "sha1") =
        CentralOutcome(CentralClient.STATUS_OK, rtt, chosenAp = ap, scores = listOf(0.3, 0.7), modelSha256 = sha, serverMs = 1.0, httpCode = 200, url = "http://c/api/v1/central_infer")

    private fun info(mode: String = "central") = BaselineSessionInfo(
        runId = "r1", terminalId = "terminal-01", experimentGroup = null, baselineMode = mode, baselineModeSource = "server",
        hflMode = "sync", hflModeSource = "app", numCycles = 3, numCyclesSource = "server", plannedMeasurements = 15,
        appType = "動画", appIndex = 1, virtualRouterId = "HFL_A24", timestampMs = 42L,
    )

    // --- 判断 ---
    @Test fun query_isSentOncePerCycle_onlyInCentralMode() {
        assertEquals(listOf(1), (1..5).filter { CentralSessionPolicy.queryDue("central", it) })
        assertTrue((1..5).none { CentralSessionPolicy.queryDue("fixed", it) })
        assertTrue((1..5).none { CentralSessionPolicy.queryDue("none", it) })
    }

    @Test fun central_neverProposesApSwitch_othersDo() {
        assertFalse(CentralSessionPolicy.shouldProposeApSwitch("central"))
        for (m in listOf("fixed", "random", "greedy")) assertTrue(m, CentralSessionPolicy.shouldProposeApSwitch(m))
    }

    @Test fun central_neverUploadsWeights_otherBaselinesKeepTheirBehavior() {
        assertFalse(CentralSessionPolicy.shouldUploadAtEnd("central"))
        for (m in listOf("fixed", "random", "greedy")) assertTrue(m, CentralSessionPolicy.shouldUploadAtEnd(m))
    }

    // --- 記録 ---
    @Test fun allOk_summaryAndPerCycleRecords() {
        val f = centralSessionFields(
            listOf(CentralCycleRecord(1, ok(10, 0, "a")), CentralCycleRecord(2, ok(30, 1, "b")), CentralCycleRecord(3, ok(20, 1, "b"))),
            "http://c",
        )
        assertEquals("ok", f.getString("central_status"))
        assertEquals(20.0, f.getDouble("central_rtt_ms"), 1e-9)
        assertEquals(1, f.getInt("central_ap")); assertEquals("b", f.getString("central_model_sha256"))
        assertEquals("http://c", f.getString("central_url"))
        assertEquals(3, f.getInt("central_queries_n")); assertEquals(3, f.getInt("central_ok_n"))
        val q = f.getJSONArray("central_queries")
        assertEquals(3, q.length())
        assertEquals(2, q.getJSONObject(1).getInt("cycle")); assertEquals(30L, q.getJSONObject(1).getLong("rtt_ms"))
        assertEquals("ok", q.getJSONObject(1).getString("status"))
    }

    @Test fun mixedFailures_statusIsFirstFailure_rttMeansOnlyOk() {
        val f = centralSessionFields(
            listOf(CentralCycleRecord(1, ok(10, 0)), CentralCycleRecord(2, CentralOutcome(CentralClient.STATUS_TIMEOUT, 5000)),
                CentralCycleRecord(3, CentralOutcome(CentralClient.STATUS_SERVER_ERROR, 7, httpCode = 503)), CentralCycleRecord(4, ok(30, 1))),
            "http://c",
        )
        assertEquals("timeout", f.getString("central_status"))
        assertEquals(20.0, f.getDouble("central_rtt_ms"), 1e-9)
        assertEquals(4, f.getInt("central_queries_n")); assertEquals(2, f.getInt("central_ok_n"))
        assertEquals(1, f.getInt("central_ap"))   // 最後に成功した推奨
        val q = f.getJSONArray("central_queries")
        assertTrue(q.getJSONObject(1).isNull("ap")); assertEquals(503, q.getJSONObject(2).getInt("http_code"))
    }

    @Test fun allFailed_hasNullsNotZeros() {
        val f = centralSessionFields(listOf(CentralCycleRecord(1, CentralOutcome(CentralClient.STATUS_UNREACHABLE, 3))), "http://c")
        assertEquals("unreachable", f.getString("central_status"))
        assertTrue(f.isNull("central_rtt_ms")); assertTrue(f.isNull("central_ap")); assertTrue(f.isNull("central_model_sha256"))
        assertEquals(0, f.getInt("central_ok_n"))
    }

    @Test fun noQueries_isExplicit() {
        val f = centralSessionFields(emptyList(), "")
        assertEquals("no_query", f.getString("central_status")); assertEquals(0, f.getInt("central_queries_n"))
    }

    @Test fun sessionRow_carriesCentralFields_andOtherModesDoNot() {
        val withCentral = buildBaselineSessionRow(info("central"), 0.4, listOf(0.5, 0.6), centralSessionFields(listOf(CentralCycleRecord(1, ok(10, 1))), "http://c"))
        assertEquals("baseline_session", withCentral.getString("row_kind")); assertEquals("central", withCentral.getString("baseline_mode"))
        assertEquals("ok", withCentral.getString("central_status")); assertEquals(0.55, withCentral.getDouble("satisfaction_baseline_mean"), 1e-12)
        assertFalse(withCentral.has("accuracy"))   // 学習の値は入れない

        val plain = buildBaselineSessionRow(info("fixed"), 0.4, listOf(0.5))
        assertTrue(plain.keys().asSequence().none { it.startsWith("central_") })   // 既存モードの行は変えない
    }

    // --- hfl-recommended-ap-mode 4.4: 判断 ---
    @Test fun hflRec_queriesOncePerCycle_proposesSwitch_neverUploads() {
        assertEquals(listOf(1), (1..5).filter { CentralSessionPolicy.queryDue("hfl_rec", it) })
        assertTrue(CentralSessionPolicy.shouldProposeApSwitch("hfl_rec"))      // central と違い提案する
        assertFalse(CentralSessionPolicy.shouldProposeApSwitch("central"))      // central は変わらない
        assertFalse(CentralSessionPolicy.shouldUploadAtEnd("hfl_rec"))
        assertEquals("/api/v1/hfl_rec_infer", CentralSessionPolicy.queryPath("hfl_rec"))
        assertEquals("/api/v1/central_infer", CentralSessionPolicy.queryPath("central"))
        assertTrue(CentralSessionPolicy.isQueryMode("hfl_rec") && CentralSessionPolicy.isQueryMode("central"))
        assertFalse(CentralSessionPolicy.isQueryMode("greedy"))
    }

    @Test fun hflRec_noProposalWhenAlreadyOnRecommendedAp() {
        assertTrue(CentralSessionPolicy.needsSwitch(recommended = 1, connected = 0))
        assertFalse(CentralSessionPolicy.needsSwitch(recommended = 1, connected = 1))
        assertTrue(CentralSessionPolicy.needsSwitch(recommended = 1, connected = null))   // 不明なら提案する（SSID 照合は呼び出し側）
    }

    // --- 4.6: hfl_rec_* と followed ---
    private fun okRec(rtt: Long, ap: Int, round: Int = 4, initial: Boolean = false, sha: String = "g1") =
        CentralOutcome(CentralClient.STATUS_OK, rtt, chosenAp = ap, scores = listOf(0.3, 0.7), modelSha256 = sha, serverMs = 1.0, httpCode = 200,
            url = "http://c/api/v1/hfl_rec_infer", round = round, isInitial = initial)

    @Test fun hflRecFields_summary() {
        val f = hflRecSessionFields(listOf(CentralCycleRecord(1, okRec(10, 0)), CentralCycleRecord(2, okRec(30, 1))), "http://c")
        assertEquals("ok", f.getString("hfl_rec_status")); assertEquals(20.0, f.getDouble("hfl_rec_rtt_ms"), 1e-9)
        assertEquals(1, f.getInt("hfl_rec_ap")); assertEquals("g1", f.getString("hfl_rec_model_sha256"))
        assertEquals(4, f.getInt("hfl_rec_round")); assertFalse(f.getBoolean("hfl_rec_is_initial"))
        assertEquals(2, f.getInt("hfl_rec_queries_n")); assertEquals(2, f.getJSONArray("hfl_rec_queries").length())
        assertFalse(f.has("central_status"))
    }

    @Test fun hflRecFields_failureHasNullsAndNoQueryIsExplicit() {
        val f = hflRecSessionFields(listOf(CentralCycleRecord(1, CentralOutcome(CentralClient.STATUS_UNREACHABLE, 3))), "http://c")
        assertEquals("unreachable", f.getString("hfl_rec_status"))
        assertTrue(f.isNull("hfl_rec_ap")); assertTrue(f.isNull("hfl_rec_rtt_ms")); assertTrue(f.isNull("hfl_rec_is_initial"))
        assertEquals("no_query", hflRecSessionFields(emptyList(), "").getString("hfl_rec_status"))
    }

    @Test fun followed_trueFalseNull() {
        assertEquals(true, hflRecFollowed("ok", 1, 1))
        assertEquals(false, hflRecFollowed("ok", 1, 0))
        assertNull(hflRecFollowed("ok", 1, null))        // 繋がった AP が不明
        assertNull(hflRecFollowed("ok", null, 1))        // 推奨なし
        assertNull(hflRecFollowed("timeout", 1, 1))      // 問い合わせに失敗したセッション
        assertNull(hflRecFollowed("no_query", null, 0))
    }

    @Test fun sessionRow_carriesHflRecFields_andOtherModesDoNot() {
        val row = buildBaselineSessionRow(info("hfl_rec"), 0.4, listOf(0.5), hflRecSessionFields(listOf(CentralCycleRecord(1, okRec(10, 1))), "http://c"))
        assertEquals("hfl_rec", row.getString("baseline_mode")); assertEquals("ok", row.getString("hfl_rec_status"))
        assertFalse(row.has("accuracy"))
        val plain = buildBaselineSessionRow(info("greedy"), 0.4, listOf(0.5))
        assertTrue(plain.keys().asSequence().none { it.startsWith("hfl_rec_") })
    }
}
