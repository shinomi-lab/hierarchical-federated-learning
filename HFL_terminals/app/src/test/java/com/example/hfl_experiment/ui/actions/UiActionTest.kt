package com.example.hfl_experiment.ui.actions

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class UiActionTest {
    private val running = UiState(isTrainingRunning = true, trainingFinished = false, isTrainingDataReady = true, isUploadingBiases = false)
    private val idle = UiState(isTrainingRunning = false, trainingFinished = false, isTrainingDataReady = true, isUploadingBiases = false)

    @Test
    fun startAction_disabledWhileRunning() {
        val start = UiAction("start", "Start 5 Cycles", ActionCategory.CORE,
            enabled = { it.isTrainingDataReady && !it.isTrainingRunning }, onInvoke = {})
        assertTrue(start.enabled(idle))
        assertFalse(start.enabled(running))
    }

    @Test
    fun action_defaultsToEnabled() {
        val a = UiAction("x", "X", ActionCategory.DEBUG, onInvoke = {})
        assertTrue(a.enabled(running))
    }
}
