package com.example.hfl_experiment.ui.actions

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * 端末の現在の研究設定（読取専用）。設定変更は adb 注入のまま（再現性維持）で、
 * UIは現在値を表示するだけ。スイープ中に各端末の条件を一目で確認できる。
 */
data class ResearchConfig(
    val terminalId: String,
    val edgeUrl: String,
    val baselineMode: String,
    val demoMode: Boolean,
    val assignedApp: String,
    val baselineModeSource: String? = null,   // "server" / "app"（null は出所を表示しない）
    val pendingBaselineMode: String? = null,  // 次セッションで変わる予定のモード
    val hflMode: String? = null,              // 現在（セッション中はそのセッション）の sync/async（null は行を出さない）
    val hflModeSource: String? = null,        // "server" / "app"
    val appHflMode: String? = null,           // 端末設定（サーバと食い違うときに表示）
    val pendingHflMode: String? = null,       // 次セッションで変わる予定の HFL モード
    val numCycles: Int? = null,               // SP3d: 現在（セッション中はそのセッション）のサイクル数（null は行を出さない）
    val numCyclesSource: String? = null,      // "server" / "app"
    val pendingNumCycles: Int? = null,        // 次セッションで変わる予定のサイクル数
    val configSource: String? = null,         // SP3d 4.2: 端末 ID・エッジ URL の出所 "manual" / "ip_auto" / null（未設定）
)

/** 表示用のラベル付き行（純関数＝テスト対象）。 */
fun formatResearchConfig(c: ResearchConfig): List<Pair<String, String>> = listOf(
    "端末ID" to c.terminalId,
    "edge_url" to c.edgeUrl,
    "baseline_mode" to formatBaselineMode(c),
    "hfl_mode" to formatHflMode(c),
    "サイクル数" to formatNumCycles(c),
    "設定の出所" to formatConfigSource(c.configSource),
    "demo_mode" to c.demoMode.toString(),
    "割当アプリ" to c.assignedApp,
).filter { !(it.first == "hfl_mode" && c.hflMode == null) && !(it.first == "サイクル数" && c.numCycles == null) }

private fun formatConfigSource(src: String?): String = when (src) {
    "manual" -> "手動（IP 自動設定は無効）"
    "ip_auto" -> "IP から自動"
    else -> "未設定"
}

private fun formatNumCycles(c: ResearchConfig): String {
    val src = when (c.numCyclesSource) { "server" -> "（サーバ指定）"; "app" -> "（端末設定）"; else -> "" }
    val pending = c.pendingNumCycles?.let { " → 次セッションから $it" } ?: ""
    return "${c.numCycles ?: ""}" + src + pending
}

private fun formatBaselineMode(c: ResearchConfig): String {
    val src = when (c.baselineModeSource) { "server" -> "（サーバ指定）"; "app" -> "（端末設定）"; else -> "" }
    val pending = c.pendingBaselineMode?.let { " → 次セッションから $it" } ?: ""
    return c.baselineMode + src + pending
}

private fun formatHflMode(c: ResearchConfig): String {
    val src = when {
        c.hflModeSource == "server" && c.appHflMode != null && c.appHflMode != c.hflMode -> "（サーバ優先・端末設定: ${c.appHflMode}）"
        c.hflModeSource == "server" -> "（サーバ指定）"
        c.hflModeSource == "app" -> "（端末設定）"
        else -> ""
    }
    val pending = c.pendingHflMode?.let { " → 次セッションから $it" } ?: ""
    return (c.hflMode ?: "") + src + pending
}

@Composable
fun ResearchStatusCard(c: ResearchConfig) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp)) {
            Text("研究設定（読取専用）", style = MaterialTheme.typography.titleSmall)
            formatResearchConfig(c).forEach { (k, v) ->
                Text("$k: $v", style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}
