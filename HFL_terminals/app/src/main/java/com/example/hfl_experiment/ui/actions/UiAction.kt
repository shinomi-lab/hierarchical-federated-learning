package com.example.hfl_experiment.ui.actions

/** 端末UIのアクションを分類するカテゴリ。セクション描画の単位。 */
enum class ActionCategory { CORE, MEASURE, RESET, TRANSFER, DEBUG, SETUP }

/**
 * ボタンの活性判定に必要な画面状態の読み取り専用スナップショット。
 * 画面描画側が現在値から構築して [UiAction.enabled] に渡す。
 */
data class UiState(
    val isTrainingRunning: Boolean,
    val trainingFinished: Boolean,
    val isTrainingDataReady: Boolean,
    val isUploadingBiases: Boolean,
)

/**
 * データ駆動のUIアクション。ボタン追加＝本データを1件足すだけ。
 *
 * @param id 安定ID（テスト・ログ用）
 * @param label 表示ラベル
 * @param category 所属カテゴリ（セクション分け）
 * @param enabled 現在状態から活性可否を返す純関数（既定は常に有効）
 * @param onInvoke クリック時の処理
 */
data class UiAction(
    val id: String,
    val label: String,
    val category: ActionCategory,
    val enabled: (UiState) -> Boolean = { true },
    val onInvoke: () -> Unit,
)
