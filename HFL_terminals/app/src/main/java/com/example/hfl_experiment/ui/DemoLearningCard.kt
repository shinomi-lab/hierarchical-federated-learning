package com.example.hfl_experiment.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

/**
 * デモ用の「見せかけ学習」カード。ネットワーク通信は一切行わず、タイマーだけで
 * ラウンド進行・進捗・精度上昇を表示する（demo_mode 時のみ MainActivity から呼ばれる）。
 *
 * 挙動: 1ラウンド = 3秒（進捗 0→100% を 2.5秒 + 「集約 ↑」0.5秒）、ラウンドは 1→8→1… の周期ループ。
 * 精度はラウンドに応じて 0.48→0.88 を繰り返す。ダッシュボード(Replay)と同じ物語・同じ3秒周期。
 *
 * コントロール（Mac 側と同じ操作感で手動同期できる）:
 *   [⏸ 停止 / ▶ 再開] … その場で一時停止・再開
 *   [⟲ 最初から]      … ラウンド1に戻して再生（3画面同時に押せば同期スタート）
 */
private const val TOTAL_ROUNDS = 8
private const val FILL_TICKS = 25          // 100ms × 25 = 2.5秒で 0→100%
private const val TICK_MS = 100L
private const val AGG_FLASH_MS = 500L      // 「集約 ↑」表示 0.5秒 → 合計 3.0秒/ラウンド
private const val PAUSE_POLL_MS = 80L

private fun accuracyFor(round: Int): Float =
    0.48f + (0.88f - 0.48f) * (round - 1) / (TOTAL_ROUNDS - 1)

@Composable
fun DemoLearningCard(terminalId: String) {
    var round by remember { mutableIntStateOf(1) }
    var progress by remember { mutableFloatStateOf(0f) }
    var accuracy by remember { mutableFloatStateOf(accuracyFor(1)) }
    var aggregating by remember { mutableStateOf(false) }
    var running by remember { mutableStateOf(true) }
    var resetSignal by remember { mutableIntStateOf(0) }

    // resetSignal が変わるとループを再起動（ラウンド1から）。running=false の間は進行を止める。
    LaunchedEffect(resetSignal) {
        round = 1; progress = 0f; accuracy = accuracyFor(1); aggregating = false
        while (true) {
            for (r in 1..TOTAL_ROUNDS) {
                round = r
                accuracy = accuracyFor(r)
                aggregating = false
                progress = 0f
                for (tick in 1..FILL_TICKS) {
                    while (!running) delay(PAUSE_POLL_MS)   // 停止中は待機
                    delay(TICK_MS)
                    progress = tick.toFloat() / FILL_TICKS
                }
                while (!running) delay(PAUSE_POLL_MS)
                aggregating = true
                delay(AGG_FLASH_MS)
            }
        }
    }

    Card(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 4.dp),
        colors = CardDefaults.cardColors(containerColor = Color(0xFF10233A)),
        elevation = CardDefaults.cardElevation(defaultElevation = 3.dp)
    ) {
        Column(modifier = Modifier.padding(14.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("学習中 · $terminalId", style = MaterialTheme.typography.titleMedium,
                    color = Color(0xFFCDE3FF), fontWeight = FontWeight.Bold)
                Text("ラウンド $round / $TOTAL_ROUNDS", style = MaterialTheme.typography.titleMedium,
                    color = Color(0xFFFFC107), fontWeight = FontWeight.Bold)
            }
            Spacer(Modifier.height(10.dp))
            LinearProgressIndicator(
                progress = { progress },
                modifier = Modifier.fillMaxWidth().height(10.dp),
                color = Color(0xFF7CF5C8),
                trackColor = Color(0xFF1E3350)
            )
            Spacer(Modifier.height(10.dp))
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("精度 ${"%.3f".format(accuracy)} ↑",
                    style = MaterialTheme.typography.titleLarge,
                    color = Color(0xFF7CF5C8), fontWeight = FontWeight.Bold)
                Text(if (aggregating) "集約 ↑" else if (running) "エッジへ集約中…" else "停止中",
                    style = MaterialTheme.typography.bodyMedium,
                    color = if (aggregating) Color(0xFFFFC107) else Color(0xFF93A1C4),
                    fontWeight = FontWeight.Bold)
            }
            Spacer(Modifier.height(12.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(onClick = { running = !running }) {
                    Text(if (running) "⏸ 停止" else "▶ 再開")
                }
                OutlinedButton(onClick = { running = true; resetSignal++ }) {
                    Text("⟲ 最初から")
                }
            }
        }
    }
}
