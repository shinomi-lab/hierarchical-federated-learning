package com.example.hfl_experiment.training

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.yield
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test
import java.util.Collections

class ReplaceableJobTest {

    @Test
    fun launchAfterCancel_waitsForPreviousToFinish() = runBlocking {
        // 旧 cancelUpload は参照を即 null にしていたため、止まりきらない旧アップロード
        // （ブロッキングI/O中）と新アップロードが並走した
        val events = Collections.synchronizedList(mutableListOf<String>())
        val slot = ReplaceableJob()
        val oldStarted = CompletableDeferred<Unit>()

        slot.launch(this) {
            try {
                oldStarted.complete(Unit)
                awaitCancellation()
            } finally {
                withContext(NonCancellable) { delay(100); events += "old-finished" }
            }
        }
        oldStarted.await()

        slot.cancel()
        slot.launch(this) { events += "new-started" }.join()

        assertEquals(listOf("old-finished", "new-started"), events)
    }

    @Test
    fun launchWhileRunning_cancelsPreviousBeforeStarting() = runBlocking {
        val events = Collections.synchronizedList(mutableListOf<String>())
        val slot = ReplaceableJob()
        val oldStarted = CompletableDeferred<Unit>()

        val first = slot.launch(this) {
            try {
                oldStarted.complete(Unit)
                awaitCancellation()
            } finally {
                events += "old-finished"
            }
        }
        oldStarted.await()
        slot.launch(this) { events += "new-started" }.join()

        assertEquals(listOf("old-finished", "new-started"), events)
        assertFalse(first.isActive)
    }

    @Test
    fun rapidTripleLaunch_neverRunsTwoBlocksConcurrently() = runBlocking {
        // A が止まりにくい I/O 中に B, C と連打 → B の待機がキャンセルされても C は A の終了を待つこと
        val events = Collections.synchronizedList(mutableListOf<String>())
        val slot = ReplaceableJob()
        val aStarted = CompletableDeferred<Unit>()

        slot.launch(this) {
            try {
                aStarted.complete(Unit)
                awaitCancellation()
            } finally {
                withContext(NonCancellable) { delay(100); events += "A-finished" }
            }
        }
        aStarted.await()
        slot.launch(this) { events += "B-started" }
        yield()
        slot.launch(this) { events += "C-started" }.join()

        assertEquals(listOf("A-finished", "C-started"), events)
    }

    @Test
    fun launch_waitsWhileOtherPathHoldsSharedMutex() = runBlocking {
        // 自動アップロード経路が同じ mutex を保持している間は、手動側の block は始まらない
        val events = Collections.synchronizedList(mutableListOf<String>())
        val slot = ReplaceableJob()
        slot.mutex.lock()
        val manual = slot.launch(this) { events += "manual-started" }
        delay(50)
        events += "auto-finished"
        slot.mutex.unlock()
        manual.join()

        assertEquals(listOf("auto-finished", "manual-started"), events)
    }

    @Test
    fun finishedJob_clearsSlot() = runBlocking {
        val slot = ReplaceableJob()
        slot.launch(this) { }.join()
        assertFalse(slot.isActive)
    }
}
