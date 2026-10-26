package com.example.hfl_experiment.ui.actions

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ResearchStatusTest {
    @Test
    fun formatsAllFieldsAsLabeledRows() {
        val c = ResearchConfig(
            terminalId = "terminal-04",
            edgeUrl = "http://172.20.10.2:8001",
            baselineMode = "none",
            demoMode = false,
            assignedApp = "通話",
        )
        val rows = formatResearchConfig(c)

        assertTrue(rows.contains("端末ID" to "terminal-04"))
        assertTrue(rows.contains("edge_url" to "http://172.20.10.2:8001"))
        assertTrue(rows.contains("baseline_mode" to "none"))
        assertTrue(rows.contains("demo_mode" to "false"))
        assertEquals("通話", rows.first { it.first == "割当アプリ" }.second)
    }

    @Test fun baselineRow_showsSourceAndPending() {
        val c = ResearchConfig(terminalId = "t", edgeUrl = "u", baselineMode = "fixed", demoMode = false, assignedApp = "a",
            baselineModeSource = "server", pendingBaselineMode = "random")
        assertTrue(formatResearchConfig(c).contains("baseline_mode" to "fixed（サーバ指定） → 次セッションから random"))
        val d = c.copy(baselineModeSource = "app", pendingBaselineMode = null)
        assertTrue(formatResearchConfig(d).contains("baseline_mode" to "fixed（端末設定）"))
    }

    @Test fun hflModeRow_showsSourceMismatchAndPending() {
        val c = ResearchConfig(terminalId = "t", edgeUrl = "u", baselineMode = "none", demoMode = false, assignedApp = "a",
            hflMode = "sync", hflModeSource = "server", appHflMode = "async")
        assertTrue(formatResearchConfig(c).contains("hfl_mode" to "sync（サーバ優先・端末設定: async）"))
        val same = c.copy(appHflMode = "sync")
        assertTrue(formatResearchConfig(same).contains("hfl_mode" to "sync（サーバ指定）"))
        val app = c.copy(hflMode = "async", hflModeSource = "app", pendingHflMode = "sync")
        assertTrue(formatResearchConfig(app).contains("hfl_mode" to "async（端末設定） → 次セッションから sync"))
    }

    @Test fun numCyclesRow_showsSourceAndPending() {
        val c = ResearchConfig(terminalId = "t", edgeUrl = "u", baselineMode = "none", demoMode = false, assignedApp = "a",
            numCycles = 20, numCyclesSource = "server", pendingNumCycles = 5)
        assertTrue(formatResearchConfig(c).contains("サイクル数" to "20（サーバ指定） → 次セッションから 5"))
        val app = c.copy(numCycles = 5, numCyclesSource = "app", pendingNumCycles = null)
        assertTrue(formatResearchConfig(app).contains("サイクル数" to "5（端末設定）"))
        val none = c.copy(numCycles = null)
        assertTrue(formatResearchConfig(none).none { it.first == "サイクル数" })
    }

    @Test fun configSourceRow() {
        val c = ResearchConfig(terminalId = "t", edgeUrl = "u", baselineMode = "none", demoMode = false, assignedApp = "a",
            configSource = "manual")
        assertTrue(formatResearchConfig(c).contains("設定の出所" to "手動（IP 自動設定は無効）"))
        assertTrue(formatResearchConfig(c.copy(configSource = "ip_auto")).contains("設定の出所" to "IP から自動"))
        assertTrue(formatResearchConfig(c.copy(configSource = null)).contains("設定の出所" to "未設定"))
    }
}

