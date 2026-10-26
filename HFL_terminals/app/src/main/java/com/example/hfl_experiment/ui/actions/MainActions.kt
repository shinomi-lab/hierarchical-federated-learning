package com.example.hfl_experiment.ui.actions

/** 学習中・バイアス送信中は押せない（モデル差し替え/ラウンド変更が進行中の処理と競合するため）。 */
private val idleOnly: (UiState) -> Boolean = { !it.isTrainingRunning && !it.isUploadingBiases }

/**
 * メイン画面の TRANSFER セクション。処理本体（WorkManager 投入等）は呼び出し側から注入する。
 */
fun transferActions(onUploadMetrics: () -> Unit): List<UiAction> = listOf(
    UiAction("upload_metrics", "メトリクスを再送", ActionCategory.TRANSFER, onInvoke = onUploadMetrics),
)

/**
 * メイン画面の RESET セクション。
 * 端末ローカルの「完全リセット」「保存ラウンドを消す」は確認ダイアログ/説明文つきで
 * TrainingScreen 側に残す（ここへ移すと確認が失われるため）。
 */
fun resetActions(onResetServerRound: () -> Unit): List<UiAction> = listOf(
    UiAction("reset_round_server", "サーバのラウンドをリセット", ActionCategory.RESET, enabled = idleOnly, onInvoke = onResetServerRound),
)

/** メイン画面の CORE セクション（TrainingScreen 外の手動モデル取得）。 */
fun coreActions(onFetchNewModel: () -> Unit): List<UiAction> = listOf(
    UiAction("fetch_new_model", "Fetch New Model (手動取得)", ActionCategory.CORE, enabled = idleOnly, onInvoke = onFetchNewModel),
)

/** メイン画面の SETUP セクション。 */
fun setupActions(onOpenSettings: () -> Unit): List<UiAction> = listOf(
    UiAction("open_settings", "環境・ネットワーク設定", ActionCategory.SETUP, onInvoke = onOpenSettings),
)
