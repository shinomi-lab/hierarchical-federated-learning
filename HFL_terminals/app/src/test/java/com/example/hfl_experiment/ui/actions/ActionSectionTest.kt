package com.example.hfl_experiment.ui.actions

import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34], qualifiers = "w411dp-h4000dp")
class ActionSectionTest {
    @get:Rule
    val rule = createComposeRule()

    private val state = UiState(isTrainingRunning = false, trainingFinished = false, isTrainingDataReady = true, isUploadingBiases = false)

    @Test
    fun collapsibleSection_startsCollapsed_andExpandsOnHeaderClick() {
        val actions = listOf(UiAction("d1", "Debug One", ActionCategory.DEBUG, onInvoke = {}))
        rule.setContent { ActionSection("デバッグ", actions, state, collapsible = true, initiallyExpanded = false) }

        rule.onNodeWithText("Debug One").assertDoesNotExist()
        rule.onNodeWithText("デバッグ").performClick()
        rule.onNodeWithText("Debug One").assertExists()
    }

    @Test
    fun debugRegistry_threeActions_allAppearAfterExpand() {
        val actions = listOf(
            UiAction("validate_dto", "Validate DTO", ActionCategory.DEBUG, onInvoke = {}),
            UiAction("debug_test_upload", "DEBUG: Test Upload to Edge", ActionCategory.DEBUG, onInvoke = {}),
            UiAction("debug_network_diagnose", "DEBUG: Network Diagnose", ActionCategory.DEBUG, onInvoke = {}),
        )
        rule.setContent { ActionSection("デバッグ", actions, state, collapsible = true, initiallyExpanded = false) }

        rule.onNodeWithText("Validate DTO").assertDoesNotExist()
        rule.onNodeWithText("デバッグ").performClick()
        rule.onNodeWithText("Validate DTO").assertExists()
        rule.onNodeWithText("DEBUG: Test Upload to Edge").assertExists()
        rule.onNodeWithText("DEBUG: Network Diagnose").assertExists()
    }

    @Test
    fun actionButton_invokesOnClick() {
        var hit = false
        val actions = listOf(UiAction("c1", "Core One", ActionCategory.CORE, onInvoke = { hit = true }))
        rule.setContent { ActionSection("操作", actions, state, collapsible = false) }

        rule.onNodeWithText("Core One").performClick()
        assertTrue(hit)
    }

    @Test
    fun expandedState_survivesActivityRecreation() {
        val restorer = StateRestorationTester(rule)
        val actions = listOf(UiAction("d1", "Debug One", ActionCategory.DEBUG, onInvoke = {}))
        restorer.setContent { ActionSection("デバッグ", actions, state, collapsible = true, initiallyExpanded = false) }

        rule.onNodeWithText("デバッグ").performClick()
        restorer.emulateSavedInstanceStateRestore()
        rule.onNodeWithText("Debug One").assertExists()
    }
}
