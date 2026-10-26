package com.example.hfl_experiment.ui.actions

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier

/**
 * カテゴリ単位の [UiAction] 群を汎用描画するセクション。
 * [collapsible] のときはヘッダ押下で展開/折りたたみを切り替える（デバッグ用途など）。
 * 各アクションは [UiAction.enabled] を [state] に適用して活性/非活性を反映する。
 */
@Composable
fun ActionSection(
    title: String,
    actions: List<UiAction>,
    state: UiState,
    collapsible: Boolean,
    initiallyExpanded: Boolean = true,
) {
    var expanded by rememberSaveable { mutableStateOf(if (collapsible) initiallyExpanded else true) }
    Column(modifier = Modifier.fillMaxWidth()) {
        if (collapsible) {
            TextButton(onClick = { expanded = !expanded }) { Text(title) }
        } else {
            Text(title)
        }
        if (expanded) {
            actions.forEach { action ->
                Button(
                    onClick = action.onInvoke,
                    enabled = action.enabled(state),
                    modifier = Modifier.fillMaxWidth(),
                ) { Text(action.label) }
            }
        }
    }
}
