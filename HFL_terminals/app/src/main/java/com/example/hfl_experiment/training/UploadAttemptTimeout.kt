package com.example.hfl_experiment.training

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.withTimeout

/**
 * アップロード1試行ぶんのタイムアウト。
 *
 * [withTimeout] の [TimeoutCancellationException] は CancellationException の派生のため、
 * そのままだとリトライ判定より先の `catch (CancellationException)` に捕まり
 * 「キャンセル」扱いで打ち切られる。試行タイムアウトは通信失敗として扱いたいので
 * [java.net.SocketTimeoutException]（= IOException, リトライ対象）に変換する。
 * 外部からのキャンセルはそのまま伝播する。
 */
suspend fun <T> withUploadAttemptTimeout(timeoutMs: Long, block: suspend CoroutineScope.() -> T): T =
    try {
        withTimeout(timeoutMs, block)
    } catch (e: TimeoutCancellationException) {
        throw java.net.SocketTimeoutException("upload attempt timed out after ${timeoutMs}ms").apply { initCause(e) }
    }
