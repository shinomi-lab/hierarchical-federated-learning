package com.example.hfl_experiment.ui.actions

import androidx.compose.foundation.layout.Column
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34], qualifiers = "w411dp-h4000dp")
class MainActionsTest {
    @get:Rule
    val rule = createComposeRule()

    private val state = UiState(isTrainingRunning = false, trainingFinished = false, isTrainingDataReady = true, isUploadingBiases = false)

    @Test
    fun transferActions_haveUploadMetricsInTransferCategory() {
        val actions = transferActions(onUploadMetrics = {})
        assertEquals(listOf("upload_metrics"), actions.map { it.id })
        assertTrue(actions.all { it.category == ActionCategory.TRANSFER })
    }

    @Test
    fun resetActions_haveServerRoundResetInResetCategory() {
        val actions = resetActions(onResetServerRound = {})
        assertEquals(listOf("reset_round_server"), actions.map { it.id })
        assertTrue(actions.all { it.category == ActionCategory.RESET })
    }

    @Test
    fun sections_renderLabels_andInvokeHandlers() {
        var uploaded = false
        var reset = false
        rule.setContent {
            Column {
                ActionSection("送信", transferActions(onUploadMetrics = { uploaded = true }), state, collapsible = false)
                ActionSection("リセット", resetActions(onResetServerRound = { reset = true }), state, collapsible = false)
            }
        }

        rule.onNodeWithText("メトリクスを再送").performClick()
        rule.onNodeWithText("サーバのラウンドをリセット").performClick()
        assertTrue(uploaded)
        assertTrue(reset)
    }

    @Test
    fun coreActions_haveFetchModelInCoreCategory() {
        val actions = coreActions(onFetchNewModel = {})
        assertEquals(listOf("fetch_new_model"), actions.map { it.id })
        assertTrue(actions.all { it.category == ActionCategory.CORE })
    }

    @Test
    fun setupActions_haveOpenSettingsInSetupCategory() {
        val actions = setupActions(onOpenSettings = {})
        assertEquals(listOf("open_settings"), actions.map { it.id })
        assertTrue(actions.all { it.category == ActionCategory.SETUP })
    }

    @Test
    fun coreAndSetupSections_renderLabels_andInvokeHandlers() {
        var fetched = false
        var opened = false
        rule.setContent {
            Column {
                ActionSection("モデル", coreActions(onFetchNewModel = { fetched = true }), state, collapsible = false)
                ActionSection("設定", setupActions(onOpenSettings = { opened = true }), state, collapsible = false)
            }
        }

        rule.onNodeWithText("Fetch New Model (手動取得)").performClick()
        rule.onNodeWithText("環境・ネットワーク設定").performClick()
        assertTrue(fetched)
        assertTrue(opened)
    }

    @Test
    fun fetchModel_isDisabledWhileTrainingOrUploading() {
        val fetch = coreActions(onFetchNewModel = {}).single()
        assertFalse(fetch.enabled(state.copy(isTrainingRunning = true)))
        assertFalse(fetch.enabled(state.copy(isUploadingBiases = true)))
        assertTrue(fetch.enabled(state))
    }

    @Test
    fun serverRoundReset_isDisabledWhileTrainingOrUploading() {
        val reset = resetActions(onResetServerRound = {}).single()
        assertFalse(reset.enabled(state.copy(isTrainingRunning = true)))
        assertFalse(reset.enabled(state.copy(isUploadingBiases = true)))
        assertTrue(reset.enabled(state))
    }
}
