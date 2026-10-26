package com.example.hfl_experiment.training

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test

class ModeSwitchProcedureTest {
    @Test fun order_isFinalizeDeliverRecord_andStopsWhenDelivered() = runBlocking {
        val log = mutableListOf<String>()
        var pending = 2
        val und = runModeSwitchProcedure(
            finalize = { log += "finalize" },
            deliver = { log += "deliver"; pending--; pending == 0 },
            pendingCount = { pending },
            record = { n -> log += "record:$n" },
            sleep = {},
        )
        assertEquals(0, und)
        assertEquals(listOf("finalize", "deliver", "deliver", "record:0"), log)
    }

    @Test fun timeout_recordsUndelivered_andProceeds() = runBlocking {
        var t = 0L
        val und = runModeSwitchProcedure(
            finalize = {}, deliver = { false }, pendingCount = { 3 }, record = {},
            timeoutMs = 10_000L, retryIntervalMs = 3_000L, now = { t }, sleep = { t += it },
        )
        assertEquals(3, und)
    }
}
