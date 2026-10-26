package com.example.hfl_experiment.ui

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34], qualifiers = "w411dp-h4000dp")
class LogSenderActivityTest {
    @get:Rule
    val rule = createAndroidComposeRule<LogSenderActivity>()

    @Test
    fun launches_andShowsUploadControls() {
        // DEBUG セクションから遷移する先が起動でき、主要操作が表示されること
        rule.onNodeWithText("Upload Selected").assertExists()
        rule.onNodeWithText("Refresh").assertExists()
    }
}
