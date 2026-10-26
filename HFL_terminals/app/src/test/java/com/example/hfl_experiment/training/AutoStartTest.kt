package com.example.hfl_experiment.training

import android.content.Intent
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.AppConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/** SP5: adb から試行を開始する入口（auto_start）。ボタンと同じ処理を呼ぶ。 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class AutoStartTest {
    @Test fun ignoresWhenRunning() =
        assertEquals(AutoStartDecision.Ignore("already_running"), decideAutoStart(isRunning = true, trainingFinished = false))

    @Test fun startsWhenIdle() =
        assertEquals(AutoStartDecision.Start, decideAutoStart(isRunning = false, trainingFinished = false))

    @Test fun startsAfterFinished() =
        assertEquals(AutoStartDecision.Start, decideAutoStart(isRunning = false, trainingFinished = true))

    @Test fun runArgsMatchButton() {
        assertEquals(RunArgs(inputSize = 2 + 4, hiddenSize = 32, outputSize = AppConfig.AP_COUNT, epochs = 5, autoUpload = true),
            buildRunArgs(appConfigsSize = 4))
        assertEquals(2 + AppConfig.APP_CAT_COUNT, buildRunArgs(appConfigsSize = null).inputSize)
    }

    @Test fun takesRequestOnce() {
        val i = Intent().putExtra("auto_start", true).putExtra("run_tag", "T001").putExtra("num_cycles", 3)
        assertEquals(AutoStartRequest(runTag = "T001", numCycles = 3), takeAutoStartRequest(i))
        assertNull(takeAutoStartRequest(i))  // 同じ intent を 2 回処理しない（画面回転など）
    }

    @Test fun noRequestWithoutExtra() {
        assertNull(takeAutoStartRequest(Intent().putExtra("run_tag", "T001")))
        assertNull(takeAutoStartRequest(null))
    }

    @Test fun invalidNumCyclesIsIgnored() {
        val i = Intent().putExtra("auto_start", true).putExtra("num_cycles", 0)
        assertEquals(AutoStartRequest(runTag = null, numCycles = null), takeAutoStartRequest(i))
    }

    // ── Task 10: 完了イベント ──
    @Test fun sessionCompleteEventHasAllKeys() {
        val ev = buildSessionCompleteEvent(SessionCompleteInfo(
            terminalId = "terminal-01", runId = "R1", runTag = "T001", result = "ok", reason = null,
            hflMode = "async", hflModeSource = "server", numCycles = 5, numCyclesSource = "server",
            baselineMode = "none", cyclesCompleted = 5, afterSwitchConfirmed = "true", startedAtMs = 1L, endedAtMs = 2L))
        for (k in listOf("kind", "type", "terminal_id", "run_id", "run_tag", "result", "reason", "hfl_mode", "hfl_mode_source",
            "num_cycles", "num_cycles_source", "baseline_mode", "cycles_completed", "after_switch_confirmed",
            "started_at_ms", "ended_at_ms")) assertTrue(k, ev.has(k))
        assertEquals("experiment_event", ev.getString("kind"))
        assertEquals("session_complete", ev.getString("type"))
        assertEquals("T001", ev.getString("run_tag"))
        assertTrue(ev.isNull("reason"))
    }

    @Test fun manualSessionHasNullRunTag() {
        val ev = buildSessionCompleteEvent(SessionCompleteInfo(
            terminalId = "t", runId = "R", runTag = null, result = "cancelled", reason = null, hflMode = "sync",
            hflModeSource = "app", numCycles = 5, numCyclesSource = "app", baselineMode = "none", cyclesCompleted = 2,
            afterSwitchConfirmed = "not_measured", startedAtMs = 1L, endedAtMs = 2L))
        assertTrue(ev.isNull("run_tag"))
    }

    // ── Task 11: AP 切り替えの確認 ──
    @Test fun confirmedWhenSsidMatches() = assertEquals("true", afterSwitchStatus("HFL_B", "HFL_B", timedOut = false))
    @Test fun confirmedWhenNoSwitchNeeded() = assertEquals("true", afterSwitchStatus("HFL_A24", null, timedOut = false))
    @Test fun falseOnTimeout() = assertEquals("false", afterSwitchStatus("HFL_A24", "HFL_B", timedOut = true))
    @Test fun unknownWithoutSsid() = assertEquals("unknown", afterSwitchStatus(null, "HFL_B", timedOut = true))
}
