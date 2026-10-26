package com.example.hfl_experiment.ui

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onRoot
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.ui.actions.ResearchConfig
import com.example.hfl_experiment.ui.actions.ResearchStatusCard
import com.github.takahirom.roborazzi.captureRoboImage
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/**
 * PC(JVM)上で画面をPNGレンダリングする視覚回帰の土台。
 * 実行: `./gradlew :app:recordRoborazziDebug` でスナップショットを生成、
 *       `:app:verifyRoborazziDebug` で差分検出。実機/エミュ不要。
 */
@RunWith(AndroidJUnit4::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [34], qualifiers = "w411dp-h4000dp")
class ScreenshotTest {

    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun trainingScreen_uploadingState() {
        composeRule.setContent {
            TrainingScreen(
                onMeasureRtt = {},
                onFetchTrainingData = {},
                onStartTraining = { _, _ -> },
                onUploadBiases = {},
                onCancelUpload = {},
                onRetryUpload = {},
                uploadProgress = 0.5f,
                uploadAttempt = 3,
                onCancelRun = {},
                onSetPollParams = { _, _ -> },
                onUploadLogFile = {},
                logUploadStatus = null,
                onClearLogUploadStatus = {},
                rttValue = 42L,
                trainingProgress = 0.4f,
                trainingFinished = true,
                isUploadingBiases = true,
                errorMessage = null,
                trainingDataStatus = "ready",
                isTrainingDataReady = true,
                isTrainingRunning = false,
                pollMaxAttempts = 10,
                pollDelayMs = 1000L,
                currentPollAttempt = 0,
            )
        }
        composeRule.onRoot().captureRoboImage()
    }

    @Test
    fun researchStatusCard_sample() {
        composeRule.setContent {
            ResearchStatusCard(
                ResearchConfig(
                    terminalId = "terminal-04",
                    edgeUrl = "http://172.20.10.2:8001",
                    baselineMode = "none",
                    demoMode = false,
                    assignedApp = "通話",
                )
            )
        }
        composeRule.onRoot().captureRoboImage()
    }
}
