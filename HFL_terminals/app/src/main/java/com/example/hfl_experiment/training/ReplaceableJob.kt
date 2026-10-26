package com.example.hfl_experiment.training

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * 同時に1本だけ走らせたいジョブ（アップロード等）のスロット。
 *
 * - [launch] は前のジョブを cancel し、[mutex] を取得してから新ブロックを実行する。
 *   排他は Mutex で保証するため、連打で待機中のジョブがキャンセルされても
 *   止まりきらない旧ジョブ（ブロッキングI/O中など）と並走しない。
 * - [mutex] を他経路（自動アップロード等）と共有すれば、それらとも排他になる。
 * - [cancel] は実行中/待機中のジョブを止める。参照はジョブ自身が終了時に、自分が最新のときだけ外す。
 *
 * メインスレッドからの呼び出しを前提とする（ViewModel から使用）。
 */
class ReplaceableJob(val mutex: Mutex = Mutex()) {
    private var current: Job? = null
    val isActive: Boolean get() = current?.isActive == true

    fun launch(scope: CoroutineScope, block: suspend CoroutineScope.() -> Unit): Job {
        current?.cancel()
        // LAZY: current を差し替えてから開始する（Main.immediate でも先に本体が走らない）
        val job = scope.launch(start = CoroutineStart.LAZY) {
            try {
                mutex.withLock { block() }
            } finally {
                if (current === coroutineContext[Job]) current = null
            }
        }
        current = job
        job.start()
        return job
    }

    fun cancel() {
        current?.cancel()
    }
}
