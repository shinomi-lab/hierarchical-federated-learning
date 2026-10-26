package com.example.hfl_experiment.util

import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class TerminalSatisfactionTest {
    @Test fun rttIndicator_usesRtt() = assertEquals(0.5f, TerminalSatisfaction.calculateByIndicator("rtt", 50f, 200f, 0f, 100f), 1e-6f)
    @Test fun tpIndicator_usesTp() = assertEquals(0.5f, TerminalSatisfaction.calculateByIndicator("tp", 5f, 10f, 10f, 0f), 1e-6f)
    @Test fun japaneseStreamingName_isRttInLegacyApi() =
        assertEquals(0.5f, TerminalSatisfaction.calculateSatisfaction("配信", 50f, 200f, 0f, 100f), 1e-6f)
}
