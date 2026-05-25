package com.example.hfl_experiment.experiment

import android.content.Context
import android.content.SharedPreferences
import java.util.UUID

/**
 * アプリ全体から参照できる実験セッション情報の一元管理 object。
 *
 * - runId       : runFiveCycles() 開始時に生成される実験セッション ID。
 *                 端末ログ・HTTP ヘッダ・サーバー JSONL をこの値で突き合わせる。
 * - terminalId  : device_config.json または SettingsActivity で設定した端末 ID。
 * - experimentGroup : device_config.json の "experiment_group"（"A"〜"D"）。
 * - currentRound    : 最後に認識したフェデレーションラウンド番号。
 * - modelVersion    : 端末が適用している global model のバージョン文字列。
 */
object ExperimentContext {

    private const val PREFS_NAME = "experiment_context"
    private const val KEY_RUN_ID = "run_id"
    private const val KEY_TERMINAL_ID = "terminal_id"
    private const val KEY_EXPERIMENT_GROUP = "experiment_group"
    private const val KEY_CURRENT_ROUND = "current_round"
    private const val KEY_MODEL_VERSION = "model_version"
    private const val KEY_RUN_STARTED_AT = "run_started_at_ms"

    @Volatile private var _runId: String = ""
    @Volatile private var _terminalId: String = ""
    @Volatile private var _experimentGroup: String = ""
    @Volatile private var _currentRound: Int = 0
    @Volatile private var _modelVersion: String = ""
    @Volatile private var _runStartedAtMs: Long = 0L

    val runId: String get() = _runId
    val terminalId: String get() = _terminalId
    val experimentGroup: String get() = _experimentGroup
    val currentRound: Int get() = _currentRound
    val modelVersion: String get() = _modelVersion
    val runStartedAtMs: Long get() = _runStartedAtMs

    /** アプリ起動時に AppConfig / device_config の値で初期化する。 */
    fun initFromAppConfig(context: Context) {
        val prefs = prefs(context)
        _terminalId = com.example.hfl_experiment.AppConfig.getTerminalId(context)
        _experimentGroup = prefs.getString(KEY_EXPERIMENT_GROUP, "") ?: ""

        // device_config_raw から experiment_group を読む
        if (_experimentGroup.isEmpty()) {
            try {
                val envPrefs = context.getSharedPreferences("env_prefs", Context.MODE_PRIVATE)
                val raw = envPrefs.getString("device_config_raw", null)
                if (!raw.isNullOrBlank()) {
                    val jo = org.json.JSONObject(raw)
                    _experimentGroup = jo.optString("experiment_group", "")
                }
            } catch (_: Exception) {}
        }

        // 前回の run_id を復元（アプリが途中で落ちた場合の継続用）
        val saved = prefs.getString(KEY_RUN_ID, "")
        if (!saved.isNullOrBlank()) {
            _runId = saved
            _runStartedAtMs = prefs.getLong(KEY_RUN_STARTED_AT, 0L)
            _currentRound = prefs.getInt(KEY_CURRENT_ROUND, 0)
            _modelVersion = prefs.getString(KEY_MODEL_VERSION, "") ?: ""
        }
    }

    /**
     * runFiveCycles() の先頭で呼ぶ。
     * 新しい実験セッションを開始し、run_id を生成して永続化する。
     */
    fun startNewRun(context: Context): String {
        val id = UUID.randomUUID().toString()
        val now = System.currentTimeMillis()
        _runId = id
        _runStartedAtMs = now
        prefs(context).edit()
            .putString(KEY_RUN_ID, id)
            .putLong(KEY_RUN_STARTED_AT, now)
            .putString(KEY_TERMINAL_ID, _terminalId)
            .apply()
        return id
    }

    /** フェデレーションラウンドが進んだ時に更新する。 */
    fun updateRound(context: Context, round: Int) {
        _currentRound = round
        prefs(context).edit().putInt(KEY_CURRENT_ROUND, round).apply()
    }

    /** 端末が適用したモデルバージョンを更新する。 */
    fun updateModelVersion(context: Context, version: String) {
        _modelVersion = version
        prefs(context).edit().putString(KEY_MODEL_VERSION, version).apply()
    }

    private fun prefs(context: Context): SharedPreferences =
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
}
