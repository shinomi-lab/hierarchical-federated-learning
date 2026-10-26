package com.example.hfl_experiment.training

import com.example.hfl_experiment.training.core.ModelParameters
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

class TrainingInitChoiceTest {
    private val own = ModelParameters(listOf(listOf(1f)), listOf(0f), listOf(1f), listOf(0f), listOf(listOf(1f)), listOf(0f), listOf(1f), listOf(0f), listOf(listOf(1f)), listOf(0f))

    @Test fun newDownloaded_winsOverOwnOrSeed() = assertNull(chooseTrainingInit("b", "a", own))   // 衝突①
    @Test fun firstDownloaded_isUsed() = assertNull(chooseTrainingInit("a", null, own))
    @Test fun sameDownloaded_continuesOwn() = assertSame(own, chooseTrainingInit("a", "a", own))  // 衝突②
    @Test fun noDownloaded_continuesOwn() = assertSame(own, chooseTrainingInit(null, null, own))
    @Test fun nothingProvided_fallsBackToAutoLoad() = assertNull(chooseTrainingInit("a", "a", null))

    // 読込に失敗したダウンロード済みモデルも「使用済み」にし、次サイクルを毎回ランダムにしない
    @Test fun usedSha_isRecordedOnDownloadedOrLoadFailure() {
        assertEquals("b", usedDownloadedSha("DOWNLOADED", "b", "a"))
        assertEquals("b", usedDownloadedSha("RANDOM_LOAD_FAILED", "b", "a"))
        assertEquals("a", usedDownloadedSha("PROVIDED", "b", "a"))
        assertEquals("a", usedDownloadedSha("RANDOM", null, "a"))
    }
}
