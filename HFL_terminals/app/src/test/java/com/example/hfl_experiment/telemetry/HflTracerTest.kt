package com.example.hfl_experiment.telemetry

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Policy ③ (d): HflTracer 純ロジック検証 (Robolectric 不要)。
 *
 * android.os.Trace は副作用 (systrace 出力) のみで戻り値テストは難しいので、
 * 主に cookie 生成の単調性と beginAsync/endAsync の対応が期待どおりかを検証。
 */
class HflTracerTest {

    @Test
    fun beginAsync_returnsMonotonicallyIncreasingCookie() {
        val c1 = HflTracer.beginAsync("HFL_test_1")
        val c2 = HflTracer.beginAsync("HFL_test_2")
        val c3 = HflTracer.beginAsync("HFL_test_3")
        // 各 cookie はユニークかつ単調増加
        assertTrue("c1 ($c1) < c2 ($c2)", c1 < c2)
        assertTrue("c2 ($c2) < c3 ($c3)", c2 < c3)
        // 同じラベルで複数回呼んでも異なる cookie が返る (区別可能)
        val c4 = HflTracer.beginAsync("HFL_test_1")
        assertNotEquals(c1, c4)
        HflTracer.endAsync("HFL_test_1", c1)
        HflTracer.endAsync("HFL_test_2", c2)
        HflTracer.endAsync("HFL_test_3", c3)
        HflTracer.endAsync("HFL_test_1", c4)
    }

    @Test
    fun trace_inline_returnsBlockResult() {
        // inline trace ラッパーが block の戻り値をそのまま返すか
        val v = HflTracer.trace("HFL_inline") {
            42 + 8
        }
        assertEquals(50, v)
    }

    @Test
    fun trace_propagatesException() {
        // block 内で throw されたら inline trace も例外を伝播 (endSection は finally で呼ばれる)
        try {
            HflTracer.trace("HFL_throw") {
                throw IllegalStateException("expected")
            }
            org.junit.Assert.fail("Expected exception not thrown")
        } catch (e: IllegalStateException) {
            assertEquals("expected", e.message)
        }
    }

    @Test
    fun markSyncStart_doesNotThrow() {
        // android.os.Trace のラッパー。例外を投げないことだけ確認
        HflTracer.markSyncStart()
    }
}
