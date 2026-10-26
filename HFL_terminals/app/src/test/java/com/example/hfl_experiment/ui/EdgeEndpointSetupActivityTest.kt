package com.example.hfl_experiment.ui

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextReplacement
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.AppConfig
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/** SP3d 4.2: エッジ接続先画面で保存したら手動設定（IP からの自動設定で上書きしない）。 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34], qualifiers = "w411dp-h4000dp")
class EdgeEndpointSetupActivityTest {
    @get:Rule val rule = createAndroidComposeRule<EdgeEndpointSetupActivity>()

    @Test fun save_marksManual() {
        rule.onNodeWithText("エッジの IP またはホスト名").performTextReplacement("10.0.0.9")
        rule.onNodeWithText("保存してアプリを開く").performScrollTo().performClick()
        assertEquals("manual", AppConfig.configSource(rule.activity))
        AppConfig.clearManualConfig(rule.activity)
    }
}
