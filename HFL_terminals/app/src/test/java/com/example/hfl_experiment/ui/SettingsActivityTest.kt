package com.example.hfl_experiment.ui

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextReplacement
import androidx.compose.ui.test.performScrollTo
import com.example.hfl_experiment.AppConfig
import org.junit.Assert.assertEquals
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34], qualifiers = "w411dp-h4000dp")
class SettingsActivityTest {
    @get:Rule
    val rule = createAndroidComposeRule<SettingsActivity>()

    @Test
    fun deadToolPocketSettings_areRemoved() {
        // どこからも読まれていなかった「ツールポケット」設定は撤去済み
        rule.onNodeWithText("UI 設定（ツールポケット）", substring = true).assertDoesNotExist()
        rule.onNodeWithText("Validate DTO をポケットに表示").assertDoesNotExist()
    }

    @Test
    fun functionalSettings_remain() {
        rule.onNodeWithText("エッジ接続先のみを確認・変更").assertExists()
        rule.onNodeWithText("保存して戻る").assertExists()
    }

    @Test
    fun baselineMode_canBeSelectedAndSaved() {
        rule.onNodeWithText("fixed（常にAP0・学習しない）").performScrollTo().performClick()
        rule.onNodeWithText("保存して戻る").performScrollTo().performClick()
        assertEquals("fixed", AppConfig.appBaselineMode)   // 設定画面が変えるのは端末設定
        AppConfig.setBaselineMode(rule.activity, "none")
    }

    @Test
    fun hflMode_canBeSelectedAndSaved() {
        rule.onNodeWithText("async（閾値ベース非同期）").performScrollTo().performClick()
        rule.onNodeWithText("保存して戻る").performScrollTo().performClick()
        assertEquals("async", AppConfig.appHflMode)         // 設定画面が変えるのは端末設定
        AppConfig.loadHflMode(rule.activity)                // 再読込後も残る
        assertEquals("async", AppConfig.appHflMode)
        AppConfig.setHflMode(rule.activity, AppConfig.DEFAULT_HFL_MODE)
    }

    @Test
    fun numCycles_canBeSavedAndRangeChecked() {
        val label = "1 セッションのサイクル数（1〜100、サーバ指定が優先）"
        rule.onNodeWithText(label).performScrollTo().performTextReplacement("20")
        rule.onNodeWithText("保存して戻る").performScrollTo().performClick()
        assertEquals(20, AppConfig.appNumCycles)
        AppConfig.setNumCycles(rule.activity, AppConfig.DEFAULT_NUM_CYCLES)
    }

    @Test
    fun numCycles_outOfRangeIsNotSaved() {
        val before = AppConfig.appNumCycles
        val label = "1 セッションのサイクル数（1〜100、サーバ指定が優先）"
        rule.onNodeWithText(label).performScrollTo().performTextReplacement("0")
        rule.onNodeWithText("保存して戻る").performScrollTo().performClick()
        assertEquals(before, AppConfig.appNumCycles)   // 保存しない（画面も閉じない）
        rule.onNodeWithText(label).performScrollTo().performTextReplacement("")
        rule.onNodeWithText("保存して戻る").performScrollTo().performClick()
        assertEquals(before, AppConfig.appNumCycles)   // 空欄も保存しない
    }

    // --- SP3d 4.2: 設定の出所（手動 > IP 自動設定） ---
    @Test
    fun modeOnlySave_doesNotMarkManual() {
        rule.onNodeWithText("fixed（常にAP0・学習しない）").performScrollTo().performClick()
        rule.onNodeWithText("保存して戻る").performScrollTo().performClick()
        org.junit.Assert.assertNull(AppConfig.configSource(rule.activity))
        AppConfig.setBaselineMode(rule.activity, "none")
    }

    @Test
    fun edgeHostChange_marksManual() {
        rule.onNodeWithText("Edge Host").performScrollTo().performTextReplacement("10.0.0.9")
        rule.onNodeWithText("保存して戻る").performScrollTo().performClick()
        assertEquals("manual", AppConfig.configSource(rule.activity))
        AppConfig.clearManualConfig(rule.activity)
    }
}

