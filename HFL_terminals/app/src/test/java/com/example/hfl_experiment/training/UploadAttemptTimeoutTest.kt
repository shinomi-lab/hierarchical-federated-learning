package com.example.hfl_experiment.training

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class UploadAttemptTimeoutTest {

    @Test
    fun attemptTimeout_isReportedAsSocketTimeout_notCancellation() = runBlocking {
        // 旧実装(withTimeout)だと TimeoutCancellationException=CancellationException となり
        // リトライ判定に到達せず「キャンセルされました」で打ち切られていた
        try {
            withUploadAttemptTimeout(50L) { delay(5_000L) }
            fail("timeout expected")
        } catch (e: CancellationException) {
            fail("timeout must not surface as cancellation: $e")
        } catch (e: java.net.SocketTimeoutException) {
            assertTrue(e is java.io.IOException)
        }
    }

    @Test
    fun blockResult_isReturnedWhenWithinTimeout() = runBlocking {
        assertEquals(42, withUploadAttemptTimeout(1_000L) { 42 })
    }

    @Test
    fun externalCancellation_stillPropagatesAsCancellation() = runBlocking {
        val started = CompletableDeferred<Unit>()
        val outcome = CompletableDeferred<Throwable?>()
        val job = launch(Dispatchers.Default) {
            try {
                withUploadAttemptTimeout(10_000L) { started.complete(Unit); awaitCancellation() }
            } catch (t: Throwable) {
                outcome.complete(t)
                throw t
            }
        }
        started.await()
        job.cancelAndJoin()
        assertTrue(outcome.await() is CancellationException)
    }
}
