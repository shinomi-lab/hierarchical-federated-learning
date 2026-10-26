package com.example.hfl_experiment.training

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** fix-audit-20261026: After の前の安定待ち（N2）と、Random の選択の記録（N3）。 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class AfterSettleAndRandomRowTest {
    @Test fun settle_isTwentySeconds() {
        assertEquals(20_000L, AFTER_SETTLE_MS)
    }

    @Test fun settle_waitsBeforeReadingStartAp() = runBlocking {
        val order = mutableListOf<String>()
        val (waited, ap) = settleThenReadAp(1234L, { ms -> order.add("wait:$ms") }) { order.add("readAp"); ConnectedAp("HFL_B", 1) }
        assertEquals(listOf("wait:1234", "readAp"), order)
        assertEquals(1234L, waited); assertEquals(1, ap.ap)
    }

    @Test fun afterRow_carriesSettleMs() {
        val row = putAfterSettle(org.json.JSONObject(), 20_000L)
        assertEquals(20_000L, row.getLong("after_settle_ms"))
    }

    @Test fun randomRow_hasChoice_otherModesDoNot() {
        assertEquals(1, randomSessionFields("random", 1)!!.getInt("random_choice_ap"))
        assertTrue(randomSessionFields("random", null)!!.isNull("random_choice_ap"))
        assertNull(randomSessionFields("fixed", 0))
        assertNull(randomSessionFields("fixed_ap1", 1))

        val info = BaselineSessionInfo(
            runId = "r", terminalId = "terminal-01", experimentGroup = null, baselineMode = "random", baselineModeSource = "server",
            hflMode = "sync", hflModeSource = "app", numCycles = 2, numCyclesSource = "server", plannedMeasurements = 10,
            appType = "通話", appIndex = 2, virtualRouterId = null, timestampMs = 1L,
        )
        val row = buildBaselineSessionRow(info, 0.9, listOf(0.9), randomSessionFields("random", 0))
        assertEquals(0, row.getInt("random_choice_ap"))
        val plain = buildBaselineSessionRow(info.copy(baselineMode = "greedy"), 0.9, listOf(0.9))
        assertFalse(plain.has("random_choice_ap"))
    }
}
