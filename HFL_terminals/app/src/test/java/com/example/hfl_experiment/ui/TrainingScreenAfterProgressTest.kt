package com.example.hfl_experiment.ui

import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/** SP3d: After の 5 回計測中は進み具合を出し、After と学習開始のボタンを受け付けない。 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34], qualifiers = "w411dp-h4000dp")
class TrainingScreenAfterProgressTest {
    @get:Rule val composeRule = createComposeRule()

    private fun setScreen(afterProgress: Pair<Int, Int>?, satisfactionAfter: Float? = null, afterN: Int? = null) {
        composeRule.setContent {
            TrainingScreen(
                onMeasureRtt = {}, onFetchTrainingData = {}, onStartTraining = { _, _ -> }, onUploadBiases = {},
                onCancelUpload = {}, onCancelRun = {}, onSetPollParams = { _, _ -> }, onUploadLogFile = {},
                logUploadStatus = null, onClearLogUploadStatus = {}, rttValue = null, trainingProgress = 1f,
                trainingFinished = true, isUploadingBiases = false, errorMessage = null, trainingDataStatus = "idle",
                isTrainingDataReady = true, isTrainingRunning = false, pollMaxAttempts = 10, pollDelayMs = 1000L,
                currentPollAttempt = 0, satisfactionAfter = satisfactionAfter,
                afterProgress = afterProgress, afterN = afterN,
            )
        }
    }

    @Test fun measuring_showsProgress_andDisablesButtons() {
        setScreen(afterProgress = 2 to 5)
        composeRule.onNodeWithText("After 計測中 2/5", substring = true).assertExists()
        composeRule.onNodeWithText("満足度を計測 (After / AP切り替え後)").assertIsNotEnabled()
        composeRule.onNodeWithText("Start 5 Cycles", substring = true).assertIsNotEnabled()
    }

    @Test fun idle_enablesButtons_andShowsMeanWithN() {
        setScreen(afterProgress = null, satisfactionAfter = 0.6f, afterN = 4)
        composeRule.onNodeWithText("After 計測中", substring = true).assertDoesNotExist()
        composeRule.onNodeWithText("満足度を計測 (After / AP切り替え後)").assertIsEnabled()
        composeRule.onNodeWithText("Start 5 Cycles", substring = true).assertIsEnabled()
        composeRule.onNodeWithText("After 平均: 0.600 (n=4)").assertExists()
    }
}
