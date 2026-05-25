// com/example/hfl_experiment/ui/TrainingScreen.kt
package com.example.hfl_experiment.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.width
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.ui.unit.dp

import kotlin.random.Random
import java.util.Locale

private const val PREFS_NAME = "hfl_prefs"
private const val KEY_AUTO_UPLOAD = "pref_auto_upload"

/**
 * Screen composable for managing and visualizing the Federated Learning training process.
 *
 * This screen allows users to:
 * - Monitor training progress and status.
 * - Fetch training data (optionally for specific apps).
 * - Start and stop the training cycle.
 * - Configure auto-upload settings.
 * - Upload trained biases manually or view auto-upload status.
 * - Adjust polling parameters for server communication.
 * - Measure RTT (Round Trip Time).
 * - Perform debug actions like clearing saved rounds.
 *
 * @param modifier Modifier to be applied to the root layout.
 * @param onMeasureRtt Callback to trigger RTT measurement.
 * @param onFetchTrainingData Callback to fetch training data. Accepts a list of app indices or null for default.
 * @param onStartTraining Callback to start the training process. Accepts selected app indices and auto-upload flag.
 * @param onUploadBiases Callback to upload trained biases to the server.
 * @param onCancelRun Callback to cancel the currently running training cycle.
 * @param onSetPollParams Callback to update polling parameters (max attempts and delay).
 * @param onClearSavedRound Optional debug callback to clear the last known round information.
 * @param onResetTrainingFinished Optional debug callback to reset the training finished flag.
 * @param rttValue The most recently measured RTT value in milliseconds, or null if not available.
 * @param trainingProgress Current training progress as a float between 0.0 and 1.0.
 * @param trainingFinished Boolean flag indicating if the training process has completed.
 * @param isUploadingBiases Boolean flag indicating if biases are currently being uploaded.
 * @param errorMessage Error message string to display if an error occurred, or null.
 * @param trainingDataStatus Status string describing the current state of training data.
 * @param isTrainingDataReady Boolean flag indicating if training data is ready for use.
 * @param isTrainingRunning Boolean flag indicating if the training process is currently active.
 * @param pollMaxAttempts The maximum number of polling attempts configured.
 * @param pollDelayMs The delay between polling attempts in milliseconds.
 * @param currentPollAttempt The current polling attempt count.
 * @param availableApps List of available application names for training selection.
 * @param initialAutoUpload Initial state for the auto-upload toggle.
 * @param downloadDirPath Optional path to display as the download directory. Defaults to app's files directory.
 */
@Composable
fun TrainingScreen(
    modifier: Modifier = Modifier, // Modifier を最初に配置
    // --- 外から渡すイベント ---
    onMeasureRtt: () -> Unit,
    onFetchTrainingData: (List<Int>?) -> Unit,
    // changed: pass autoUpload flag from UI
    onStartTraining: (List<Int>, Boolean) -> Unit,
    onUploadBiases: () -> Unit,
    // new optional callback for satisfaction measurement (backward compatible)
    onMeasureSatisfaction: ((List<Int>?) -> Unit)? = null,
    onMeasureSatisfactionBefore: ((List<Int>?) -> Unit)? = null,
    onMeasureSatisfactionAfter: ((List<Int>?) -> Unit)? = null,
    // optional upload controls: nullable so callers that don't provide them (older MainActivity) still work
    onCancelUpload: (() -> Unit)? = null,
    onRetryUpload: (() -> Unit)? = null,
    // upload progress values (0f..1f) and attempt count for UI; default values keep backward compatibility
    uploadProgress: Float = 0f,
    uploadAttempt: Int = 0,
    onCancelRun: () -> Unit,
    // 学習停止 + 完全リセット（ラウンド・セッション・進捗をすべてクリア）
    onFullReset: (() -> Unit)? = null,
    onSetPollParams: (Int, Long) -> Unit,
    // debug helper: clear persisted last_known_round
    onClearSavedRound: (() -> Unit)? = null,
    // optional debug helper: allow resetting trainingFinished flag from UI
    onResetTrainingFinished: (() -> Unit)? = null,
    // Optional: caller can provide a composable hook to trigger a forced network switch.
    // This keeps the forced-switch as a composable UI element instead of hardcoding it in Activity.
    onForcedSwitch: (() -> Unit)? = null,
    // NEW: log file upload
    onUploadLogFile: () -> Unit,
    logUploadStatus: String?,
    onClearLogUploadStatus: () -> Unit,
    // NEW: indicate whether a log upload is in progress so UI can disable button
    isLogUploading: Boolean = false,

    // --- 外から渡す状態 ---
    rttValue: Long?,
    // Changed: accept progress as Float (0f..1f) so updates are driven by StateFlow<Float>
    trainingProgress: Float,
    trainingFinished: Boolean,
    isUploadingBiases: Boolean,
    errorMessage: String?,
    trainingDataStatus: String,
    isTrainingDataReady: Boolean,
    isTrainingRunning: Boolean,
    // cycle/epoch reporting
    currentCycle: Int = 0,
    totalCycles: Int = 5,
    currentEpoch: Int = 0,
    epochsPerCycle: Int = 1,
    // polling UI values
    pollMaxAttempts: Int,
    pollDelayMs: Long,
    currentPollAttempt: Int,
    // app selection: JSON から読み込んだアプリ名のリストを渡す
    availableApps: List<String>? = null,

    // NEW: optional assigned app index from ViewModel (one-time assigned per device). When provided,
    // the UI will prefer and show this index as the default selection instead of using a local random.
    assignedAppIndex: Int? = null,

    // initial persisted value for auto-upload (MainActivity should read prefs and pass it)
    initialAutoUpload: Boolean = false,

    // 渡されなければ自動で filesDir を使う
    downloadDirPath: String? = null,

    // NEW: optional satisfaction display values (学習前/学習後)
    satisfactionBefore: Float? = null,
    satisfactionAfter: Float? = null,
) {
    // display percent text from float
    val percentText = remember(trainingProgress) { "${(trainingProgress * 100).toInt()}%" }

    // Smooth animations for progress bars to make UI feel responsive without heavy updates
    val animatedTrainingProgress = animateFloatAsState(targetValue = trainingProgress.coerceIn(0f, 1f), animationSpec = tween(durationMillis = 300))
    val animatedUploadProgress = animateFloatAsState(targetValue = uploadProgress.coerceIn(0f, 1f), animationSpec = tween(durationMillis = 300))

    // ★ 未指定なら端末の内部保存先を自動で採用
    val context = LocalContext.current
    val fallbackPath = remember { context.filesDir.absolutePath }
    val pathToShow = (downloadDirPath ?: fallbackPath).ifBlank { fallbackPath }

    // State for showing log upload result dialog
    var showLogUploadResultDialog by remember { mutableStateOf(false) }
    val logUploadMessage = remember { mutableStateOf("") }

    // State for showing full-reset confirmation dialog
    var showFullResetDialog by remember { mutableStateOf(false) }

    LaunchedEffect(logUploadStatus) {
        if (logUploadStatus != null) {
            showLogUploadResultDialog = true
            logUploadMessage.value = logUploadStatus
        }
    }


    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        Text(
            text = "Training Progress: $percentText",
            style = MaterialTheme.typography.bodyLarge
        )

        // Show cycle/epoch status when available
        if (currentCycle > 0) {
            Text(
                text = "サイクル ${currentCycle}/${totalCycles} ／ エポック ${currentEpoch}/${epochsPerCycle}",
                style = MaterialTheme.typography.bodyMedium
            )
        }

        // use the Float progress directly so Compose recompose happens on change
        LinearProgressIndicator(
            progress = animatedTrainingProgress.value, // smooth animated progress
            modifier = Modifier
                .fillMaxWidth()
                .height(6.dp)
        )

        if (trainingFinished) {
            Text(
                text = "✅ Training Complete! (���習完了)",
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.primary
            )
        }

        errorMessage?.let {
            Text(
                text = "Error: $it",
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodyMedium
            )
        }

        HorizontalDivider(Modifier.padding(vertical = 8.dp))

        // --- Training Data ---
        Text(
            text = "Training Data Status: $trainingDataStatus",
            style = MaterialTheme.typography.bodyMedium
        )

        // ★ 保存先ディレクトリの表示＋コピー（必ず表示される）
        val clipboardManager = LocalContext.current.getSystemService(ClipboardManager::class.java)
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            Text("Save dir:", style = MaterialTheme.typography.bodyMedium)
            SelectionContainer(
                modifier = Modifier.weight(1f)
            ) {
                Text(
                    text = pathToShow,
                    style = MaterialTheme.typography.bodySmall,
                    maxLines = 2
                )
            }
            TextButton(onClick = {
                // 修正: clipboard.setText を clipboardManager.setPrimaryClip に変更
                clipboardManager?.setPrimaryClip(ClipData.newPlainText("Copied Text", pathToShow))
            }) {
                Text("Copy")
            }
        }

        Button(
            onClick = { onFetchTrainingData(null) },
            modifier = Modifier.fillMaxWidth(),
            enabled = !trainingDataStatus.contains("ダウンロード中")
        ) { Text("Fetch Training Data") }

        HorizontalDivider(Modifier.padding(vertical = 8.dp)) // Divider を置き換え

        // --- App selection checkboxes ---
        Spacer(Modifier.height(8.dp))
        Text("Select apps to include in training:")
        // initialize selected list and default random selection
        // use provided app list if available, otherwise fallback to generic names
        val appsToShow = availableApps ?: List(5) { index -> "App ${index + 1}" }

        // remember a randomly chosen default selection index and a disabled flag
        // prefer assignedAppIndex passed from ViewModel when available; otherwise fall back to a random default
        val defaultSelectedIndex = remember {
            if (appsToShow.isNotEmpty()) {
                assignedAppIndex?.coerceIn(0, appsToShow.size - 1) ?: Random.nextInt(appsToShow.size)
            } else -1
        }
        val disableAppSelection = remember { mutableStateOf(true) }

        val selected = remember { mutableStateListOf<Int>() }
        // initialize selected once with default if empty
        LaunchedEffect(Unit) {
            if (selected.isEmpty() && defaultSelectedIndex >= 0) {
                selected.add(defaultSelectedIndex)
            }
        }

        Column(modifier = Modifier.fillMaxWidth()) {
            appsToShow.forEachIndexed { index, name ->
                Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    val checked = selected.contains(index)
                    Checkbox(checked = checked, onCheckedChange = { new ->
                        if (!disableAppSelection.value) {
                            if (new) selected.add(index) else selected.remove(index)
                        }
                    }, enabled = !disableAppSelection.value)
                    Text(
                        text = "${index + 1}. $name" + if (index == defaultSelectedIndex) " (デフォルト選択)" else "",
                        style = if (disableAppSelection.value && index == defaultSelectedIndex) MaterialTheme.typography.bodyMedium else MaterialTheme.typography.bodySmall
                    )
                }
            }
            TextButton(onClick = {
                // allow fetching data for selected apps (or null to use defaults)
                onFetchTrainingData(if (selected.isEmpty()) null else selected.toList())
            }) {
                Text("Fetch Training Data for selected apps")
            }
        }

        // Auto-upload toggle (persistent)
        val ctxLocal = LocalContext.current
        val prefs = ctxLocal.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        var autoUpload by remember { mutableStateOf(initialAutoUpload || prefs.getBoolean(KEY_AUTO_UPLOAD, false)) }
        Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
            Text("Auto-upload after 5 cycles")
            // Place the forced switch button next to auto-upload toggle when provided
            onForcedSwitch?.let { callback ->
                Spacer(modifier = Modifier.width(8.dp))
                // Use the shared composable from MainActivity; caller should pass Activity's triggerForcedSwitch
                ForcedSwitchButton(onClick = callback, modifier = Modifier.width(200.dp))
            }
        }

        Button(
            onClick = { onStartTraining(selected.toList(), autoUpload) },
            modifier = Modifier.fillMaxWidth(),
            // 学習完了フラグが true でも再実行を許可するため、trainingFinished の条件を外す
            enabled = isTrainingDataReady && !isTrainingRunning
        ) { Text("Start 5 Cycles (5回連続実行)") }

        // --- Upload Biases ---
        // Upload button with inline progress indicator when uploading
        Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(
                onClick = onUploadBiases,
                modifier = Modifier.fillMaxWidth(),
                enabled = trainingFinished && !isUploadingBiases
            ) {
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
                    Text("Upload Biases (バイアスをアップロード)")
                    if (isUploadingBiases) {
                        Spacer(modifier = Modifier.width(8.dp))
                        CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                    }
                }
            }

            if (isUploadingBiases) {
                // show progress bar and attempt count
                LinearProgressIndicator(progress = { animatedUploadProgress.value }, modifier = Modifier.fillMaxWidth().height(6.dp))
                Text("Upload progress: ${(animatedUploadProgress.value * 100).toInt()}% (attempt $uploadAttempt)")
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = { onCancelUpload?.invoke() }, modifier = Modifier.weight(1f)) { Text("Cancel Upload") }
                    Button(onClick = { onRetryUpload?.invoke() }, modifier = Modifier.weight(1f)) { Text("Retry Now") }
                }
            }
        }

        // show a helpful toast when training finishes to clarify upload behavior
        LaunchedEffect(trainingFinished, isUploadingBiases) {
            if (trainingFinished) {
                val msg = if (isUploadingBiases) "学習完了。自動アップロード中です。" else "学習完了。Upload ボタンを押して手動で送信してください（Auto-upload を ON にすると自動送信します）。"
                android.widget.Toast.makeText(ctxLocal, msg, android.widget.Toast.LENGTH_LONG).show()
            }
        }

        // --- Stop / Cancel（学習がおかしいときは先にここで止める）---
        Button(
            onClick = onCancelRun,
            modifier = Modifier.fillMaxWidth(),
            enabled = isTrainingRunning
        ) { Text("学習を中止（実行中の5サイクルを中断）") }

        Spacer(Modifier.height(4.dp))
        Text(
            text = "学習完了を待たずにエッジへ状況ログを送れます。中止後に押すのがおすすめです。",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )

        // --- 完全リセット ---
        Spacer(Modifier.height(8.dp))
        if (onFullReset != null) {
            Button(
                onClick = { showFullResetDialog = true },
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(
                    containerColor = MaterialTheme.colorScheme.error
                )
            ) { Text("学習停止・完全リセット") }
            Text(
                text = "学習を停止し、ラウンド番号・セッション・進捗をすべて初期化します。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        // 完全リセット確認ダイアログ
        if (showFullResetDialog) {
            AlertDialog(
                onDismissRequest = { showFullResetDialog = false },
                title = { Text("完全リセットの確認") },
                text = { Text("学習を停止し、ラウンド番号・セッションID・学習進捗をすべてリセットします。\nサーバ接続の確認画面に戻り、新しい試行を開始できます。\n\nこの操作は元に戻せません。続けますか？") },
                confirmButton = {
                    TextButton(
                        onClick = {
                            showFullResetDialog = false
                            onFullReset?.invoke()
                        }
                    ) { Text("リセット", color = MaterialTheme.colorScheme.error) }
                },
                dismissButton = {
                    TextButton(onClick = { showFullResetDialog = false }) { Text("キャンセル") }
                }
            )
        }

        // エラーレポート: RealTimeLogger / training_log 等のスナップショットをエッジ upload_client_logs へ
        Spacer(Modifier.height(8.dp))
        OutlinedButton(
            onClick = { onUploadLogFile() },
            modifier = Modifier.fillMaxWidth(),
            enabled = !isLogUploading
        ) { Text("エラーレポート送信（ログをエッジへ）") }

        // Show small progress indicator next to upload button if active
        if (isLogUploading) {
            Spacer(Modifier.height(8.dp))
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
                Spacer(Modifier.width(8.dp))
                Text("Uploading logs...", style = MaterialTheme.typography.bodySmall)
            }
        }

        // Show Log Upload Result Dialog
        if (showLogUploadResultDialog) {
            val msg = logUploadMessage.value
            val treatAsSuccess = msg.startsWith("Upload successful", ignoreCase = true)
                || msg.startsWith("Upload queued", ignoreCase = true)
                || msg.contains("キュー", ignoreCase = true)
                || msg.contains("enqueued", ignoreCase = true)
            AlertDialog(
                onDismissRequest = { showLogUploadResultDialog = false; onClearLogUploadStatus() },
                title = { Text(if (treatAsSuccess) context.getString(com.example.hfl_experiment.R.string.dialog_logs_uploaded_title) else context.getString(com.example.hfl_experiment.R.string.dialog_logs_uploaded_error)) },
                text = { Text(logUploadMessage.value) },
                confirmButton = {
                    TextButton(onClick = { showLogUploadResultDialog = false; onClearLogUploadStatus() }) { Text("OK") }
                }
            )
        }

        // 試行をまたぐとき: アプリの「キャッシュ削除」(OS設定)は基本不要。状態は下のボタンで揃える。
        if (onResetTrainingFinished != null || onClearSavedRound != null) {
            HorizontalDivider(Modifier.padding(vertical = 8.dp))
            Text(
                text = "試行の前後で状態を合わせる（キャッシュ削除ではなくここ）",
                style = MaterialTheme.typography.titleSmall
            )
            Text(
                text = "・「保存ラウンドを消す」… 端末が覚えているサーバラウンドを忘れ、次のメタを新ラウンドとして扱う\n" +
                    "・「学習完了をやり直す」… 学習完了の表示を消して「Start 5 Cycles」を押せるようにする",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        // Debug helper: clear persisted saved round to force treating next meta as new
        if (onClearSavedRound != null) {
            OutlinedButton(
                onClick = { onClearSavedRound.invoke() },
                modifier = Modifier.fillMaxWidth()
            ) { Text("保存ラウンドを消す（Clear saved round）") }
        }

        // Debug helper: allow manual clearing of trainingFinished to force re-run
        if (onResetTrainingFinished != null) {
            OutlinedButton(
                onClick = { onResetTrainingFinished.invoke() },
                modifier = Modifier.fillMaxWidth()
            ) { Text("学習完了をやり直す（Reset finished flag）") }
        }

        // --- Polling settings UI ---
        Spacer(Modifier.height(8.dp))
        Text("Polling settings:")
        // keep attempt/delay text state at Column scope so both Rows can access them
        var attemptsText by remember { mutableStateOf(pollMaxAttempts.toString()) }
        var delayText by remember { mutableStateOf(pollDelayMs.toString()) }
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(
                value = attemptsText,
                onValueChange = { attemptsText = it },
                label = { Text("Max Attempts") },
                modifier = Modifier.weight(1f)
            )

            OutlinedTextField(
                value = delayText,
                onValueChange = { delayText = it },
                label = { Text("Delay ms") },
                modifier = Modifier.weight(1f)
            )
        }
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                val a = attemptsText.toIntOrNull() ?: pollMaxAttempts
                val d = delayText.toLongOrNull() ?: pollDelayMs
                onSetPollParams(a, d)
            }) { Text("Apply Poll Params") }

            Text("Current attempt: $currentPollAttempt", modifier = Modifier.align(Alignment.CenterVertically))
        }

        // --- RTT ---
        Spacer(Modifier.height(16.dp))
        HorizontalDivider(Modifier.height(16.dp))
        Spacer(Modifier.height(8.dp))

        Button(onClick = onMeasureRtt, modifier = Modifier.fillMaxWidth()) {
            Text("Measure RTT (RTT を測定)")
        }
        Text("RTT: ${rttValue?.let { "$it ms" } ?: "--"}")

        // --- 端末満足度 ---
        Spacer(Modifier.height(8.dp))
        HorizontalDivider()
        Spacer(Modifier.height(8.dp))

        // Before: 学習前のみ有効（学習中・完了後はdisable）
        // ※ runFiveCycles() 開始時に自動計測されるため、通常は手動操作不要
        Button(
            onClick = {
                val args = if (selected.isEmpty()) null else selected.toList()
                onMeasureSatisfactionBefore?.invoke(args)
            },
            enabled = !isTrainingRunning && !trainingFinished,
            modifier = Modifier.fillMaxWidth()
        ) {
            Text("満足度を計測 (Before / 学習前)  ※学習開始時に自動計測")
        }
        satisfactionBefore?.let {
            Text(
                "Before: ${"%.3f".format(it)}",
                style = MaterialTheme.typography.bodyMedium,
                color = when {
                    it > 0.8f -> androidx.compose.ui.graphics.Color(0xFF2E7D32)
                    it < 0.5f -> androidx.compose.ui.graphics.Color(0xFFD32F2F)
                    else -> MaterialTheme.colorScheme.onSurface
                }
            )
        }

        Spacer(Modifier.height(8.dp))

        // 学習完了後にAP切り替えを促すカード
        if (trainingFinished) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = androidx.compose.ui.graphics.Color(0xFFFFF8E1))
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    Text("⚡ 学習完了", style = MaterialTheme.typography.titleSmall)
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "AP切り替えを実施してから「満足度を計測 (After)」を押してください。",
                        style = MaterialTheme.typography.bodySmall
                    )
                }
            }
            Spacer(Modifier.height(8.dp))
        }

        // After: 学習完了後のみ有効
        Button(
            onClick = {
                val args = if (selected.isEmpty()) null else selected.toList()
                onMeasureSatisfactionAfter?.invoke(args)
            },
            enabled = trainingFinished,
            modifier = Modifier.fillMaxWidth()
        ) {
            Text("満足度を計測 (After / AP切り替え後)")
        }
        satisfactionAfter?.let {
            Text(
                "After: ${"%.3f".format(it)}",
                style = MaterialTheme.typography.bodyMedium,
                color = when {
                    it > 0.8f -> androidx.compose.ui.graphics.Color(0xFF2E7D32)
                    it < 0.5f -> androidx.compose.ui.graphics.Color(0xFFD32F2F)
                    else -> MaterialTheme.colorScheme.onSurface
                }
            )
        }
        // Before→After の改善量を表示
        if (satisfactionBefore != null && satisfactionAfter != null) {
            val delta = satisfactionAfter - satisfactionBefore
            val sign = if (delta >= 0) "+" else ""
            Text(
                "改善量: ${sign}${"%.3f".format(delta)}",
                style = MaterialTheme.typography.bodyMedium,
                color = if (delta >= 0) androidx.compose.ui.graphics.Color(0xFF2E7D32) else androidx.compose.ui.graphics.Color(0xFFD32F2F)
            )
        }

    }
}

/**
 * A button that triggers a forced network switch when clicked.
 *
 * This composable is intended to be used for testing and debugging purposes,
 * allowing developers to simulate network changes without modifying the device settings.
 *
 * @param onClick The callback to be invoked when the button is clicked.
 * @param modifier Optional. [Modifier] to be applied to the button.
 */
@androidx.compose.runtime.Composable
fun ForcedSwitchButton(onClick: () -> Unit, modifier: Modifier = Modifier) {
    Button(onClick = onClick, modifier = modifier) {
        Text("強制切替（即試行）")
    }
}
