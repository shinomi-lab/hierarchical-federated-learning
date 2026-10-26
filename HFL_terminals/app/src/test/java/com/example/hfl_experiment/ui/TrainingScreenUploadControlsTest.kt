package com.example.hfl_experiment.ui

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * Regression guard for the bias-upload controls in [TrainingScreen].
 *
 * Runs on the JVM via Robolectric (no device/emulator). The "Retry Now" button
 * and the upload-progress block must only appear while a bias upload is in
 * flight, and the retry button must forward to onRetryUpload. (MainActivity
 * wires onRetryUpload -> TrainingViewModel.uploadUpdate and passes the real
 * uploadProgress/uploadAttempt flows; this test locks the screen's half.)
 */
@RunWith(AndroidJUnit4::class)
// 長い非スクロール画面を全てレイアウト内に収め、performClick が着弾するよう十分高い仮想画面にする
@Config(sdk = [34], qualifiers = "w411dp-h4000dp")
class TrainingScreenUploadControlsTest {

    @get:Rule
    val composeRule = createComposeRule()

    private fun setScreen(
        isUploadingBiases: Boolean,
        onRetryUpload: (() -> Unit)? = null,
    ) {
        composeRule.setContent {
            TrainingScreen(
                onMeasureRtt = {},
                onFetchTrainingData = {},
                onStartTraining = { _, _ -> },
                onUploadBiases = {},
                onCancelUpload = {},
                onRetryUpload = onRetryUpload,
                uploadProgress = 0.5f,
                uploadAttempt = 3,
                onCancelRun = {},
                onSetPollParams = { _, _ -> },
                onUploadLogFile = {},
                logUploadStatus = null,
                onClearLogUploadStatus = {},
                rttValue = null,
                trainingProgress = 0f,
                trainingFinished = true,
                isUploadingBiases = isUploadingBiases,
                errorMessage = null,
                trainingDataStatus = "idle",
                isTrainingDataReady = true,
                isTrainingRunning = false,
                pollMaxAttempts = 10,
                pollDelayMs = 1000L,
                currentPollAttempt = 0,
            )
        }
    }

    @Test
    fun retryButton_invokesCallback_whileUploading() {
        var retried = false
        setScreen(isUploadingBiases = true, onRetryUpload = { retried = true })

        composeRule.onNodeWithText("Retry Now").assertExists()
        composeRule.onNodeWithText("Retry Now").performClick()

        assertTrue("Retry Now should invoke onRetryUpload", retried)
    }

    @Test
    fun retryButton_hidden_whenNotUploading() {
        setScreen(isUploadingBiases = false, onRetryUpload = {})

        composeRule.onNodeWithText("Retry Now").assertDoesNotExist()
    }
}
