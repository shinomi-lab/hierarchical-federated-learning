// com/example/hfl_experiment/training/TrainingViewModel.kt
package com.example.hfl_experiment.training

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.example.hfl_experiment.network.NetworkClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray
import org.json.JSONObject
import com.example.hfl_experiment.training.core.LocalTrainer
import com.example.hfl_experiment.training.core.ModelParameters
import com.example.hfl_experiment.training.data.DataSample
import com.example.hfl_experiment.util.Timing
import com.example.hfl_experiment.AppConfig
import android.content.Context
import androidx.core.content.edit
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.isActive
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import com.example.hfl_experiment.network.ModelUpdateUtil
import com.example.hfl_experiment.model.AppConfig as AppConfigModel
import java.util.concurrent.atomic.AtomicBoolean
import java.util.ArrayDeque
import kotlinx.coroutines.channels.Channel
import java.lang.Exception
import com.example.hfl_experiment.network.api.MetaResp
import android.os.BatteryManager
import android.net.NetworkCapabilities
import com.example.hfl_experiment.util.logging.ServerStyleLogger
import java.io.File
import com.example.hfl_experiment.util.logging.RealTimeLogger
import com.example.hfl_experiment.telemetry.TelemetrySender
import com.example.hfl_experiment.training.TrainingMetricsStore.TrainingMetric



class TrainingViewModel(application: Application) : AndroidViewModel(application) {
    init {
        try { ServerStyleLogger.init(getApplication()) } catch (_: Exception) {}
        try {
            // Ensure a one-time assigned app index exists at startup (uses default app count)
            ensureAssignedAppInitialized(AppConfig.APP_CAT_COUNT)
        } catch (_: Exception) {}
    }

    private companion object {
        private const val TAG = "TrainingViewModel"
        private const val MAX_UPLOAD_RETRIES = 3
        private const val BASE_UPLOAD_RETRY_DELAY_MS = 500L
        private const val UPLOAD_TIMEOUT_MS = 120_000L
    }

    private var decisionEngine: com.example.hfl_experiment.experiment.DecisionEngine? = null
    private val terminalId: String = AppConfig.getTerminalId(getApplication())
    private val edgeBaseUrl: String = AppConfig.getEdgeBaseUrl(getApplication())

    private val _trainingProgress = MutableStateFlow(0f)
    val trainingProgress: StateFlow<Float> = _trainingProgress

    private val _trainingFinished = MutableStateFlow(false)
    val trainingFinished: StateFlow<Boolean> = _trainingFinished

    private val _isUploading = MutableStateFlow(false)
    val isUploading: StateFlow<Boolean> = _isUploading

    private val _uploadProgress = MutableStateFlow(0f)
    @Suppress("unused")
    val uploadProgress: StateFlow<Float> = _uploadProgress

    private val _uploadAttempt = MutableStateFlow(0)
    @Suppress("unused")
    val uploadAttempt: StateFlow<Int> = _uploadAttempt

    private val _errorMessage = MutableStateFlow<String?>(null)
    val errorMessage: StateFlow<String?> = _errorMessage

    private val _trainingDataStatus = MutableStateFlow("待機中")
    val trainingDataStatus: StateFlow<String> = _trainingDataStatus

    private val _isTrainingDataReady = MutableStateFlow(false)
    val isTrainingDataReady: StateFlow<Boolean> = _isTrainingDataReady

    private val _isTrainingRunning = MutableStateFlow(false)
    val isTrainingRunning: StateFlow<Boolean> = _isTrainingRunning

    // --- Assigned app index (one-time per device) ---
    private val _assignedAppIndex = MutableStateFlow<Int?>(null)
    val assignedAppIndex: StateFlow<Int?> = _assignedAppIndex.asStateFlow()

    private fun persistAssignedAppIndex(idx: Int) {
        try {
            val sp = getApplication<Application>().getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
            sp.edit().putInt("assigned_app_index", idx).apply()
            RealTimeLogger.d(TAG, "persistAssignedAppIndex: saved assigned_app_index=$idx")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "persistAssignedAppIndex failed: ${e.message}")
        }
    }

    private fun readAssignedAppIndex(): Int? {
        return try {
            val sp = getApplication<Application>().getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
            if (sp.contains("assigned_app_index")) {
                val v = sp.getInt("assigned_app_index", -1)
                if (v >= 0) v else null
            } else null
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "readAssignedAppIndex failed: ${e.message}")
            null
        }
    }

    private fun ensureAssignedAppInitialized(appNumMax: Int) {
        try {
            if (_assignedAppIndex.value == null) {
                // Check if user manually selected an app type in setup screen
                val saved = readAssignedAppIndex()
                if (saved != null && saved < appNumMax) {
                    _assignedAppIndex.value = saved
                    RealTimeLogger.i(TAG, "ensureAssignedAppInitialized: using user-selected assigned_app_index=${saved} appType=${appConfigs?.getOrNull(saved)?.appType}")
                } else {
                    // Pick a fresh random app type
                    val pick = kotlin.random.Random.nextInt(0, appNumMax.coerceAtLeast(1))
                    _assignedAppIndex.value = pick
                    persistAssignedAppIndex(pick)
                    RealTimeLogger.i(TAG, "ensureAssignedAppInitialized: picked assigned_app_index=${pick} appType=${appConfigs?.getOrNull(pick)?.appType} (appNumMax=${appNumMax})")
                }
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "ensureAssignedAppInitialized failed: ${e.message}")
            if (_assignedAppIndex.value == null) _assignedAppIndex.value = 0
        }
    }

    private val _pollMaxAttempts = MutableStateFlow(20)
    val pollMaxAttempts: StateFlow<Int> = _pollMaxAttempts

    private val _pollDelayMs = MutableStateFlow(15_000L)
    val pollDelayMs: StateFlow<Long> = _pollDelayMs

    private val _currentPollAttempt = MutableStateFlow(0)
    val currentPollAttempt: StateFlow<Int> = _currentPollAttempt

    private var localTrainer: LocalTrainer? = null
    private val networkClient = NetworkClient(
        context = getApplication(),
        baseUrl = edgeBaseUrl,
        authToken = AppConfig.getServerAuthToken(getApplication())
    )
    private var fullDataset: List<DataSample> = emptyList()

    private var appConfigs: List<AppConfigModel>? = null
    // record-connected-ap: 以前は固定の "HFL_A24" で、どの AP に繋がっていたかを記録できなかった。
    // 記録・アップロードの時点で実際に繋がっている SSID を読む（取れなければ null / "unknown"。AP0 に置き換えない）。
    fun readConnectedAp(): ConnectedAp = resolveConnectedAp(
        try { com.example.hfl_experiment.util.networking.NetworkSwitchHelper.getConnectedSsid(getApplication()) } catch (_: Exception) { null },
        AppConfig.getPrimarySsid(getApplication()), AppConfig.getSecondarySsid(getApplication()),
        ip = com.example.hfl_experiment.util.networking.NetworkSwitchHelper.getWifiIpv4(getApplication()),   // ap-detection-fallback: SSID が隠されるときのサブネット判定
        prefixA = AppConfig.getSubnetA(getApplication()), prefixB = AppConfig.getSubnetB(getApplication())
    )
    val currentRouterId: String get() = routerIdOf(readConnectedAp(), AppConfig.getPrimarySsid(getApplication()), AppConfig.getSecondarySsid(getApplication()))

    fun setAppConfigs(configs: List<AppConfigModel>?) {
        appConfigs = configs
        val size = configs?.size ?: 0
        RealTimeLogger.d(TAG, "App configs set: size=${size}")
        if (size == 0) {
            RealTimeLogger.w(TAG, "No app.json provided — falling back to local defaults (AppConfig.APP_CAT_COUNT=${AppConfig.APP_CAT_COUNT})")
        }
    }

    private var lastKnownRound: Int? = null
    private val PREFS_NAME_ROUND = "training_prefs"
    private val PREFS_KEY_LAST_ROUND = "last_known_round"

    private fun persistLastKnownRound(r: Int) {
        try {
            val sp = getApplication<Application>().getSharedPreferences(PREFS_NAME_ROUND, Context.MODE_PRIVATE)
            sp.edit { putInt(PREFS_KEY_LAST_ROUND, r) }
            RealTimeLogger.d(TAG, "persistLastKnownRound: saved last_known_round=$r")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "persistLastKnownRound failed: ${e.message}")
        }
    }

    private suspend fun ensureActive() {
        val _ctx = currentCoroutineContext()
        if (_ctx[Job]?.isActive != true) throw CancellationException()
    }

    private var modelInputSize = 0
    private var modelHiddenSize = 0
    private var modelOutputSize = 0

    private var lastUploadedModelParameters: ModelParameters? = null
    private var awaitingModelAfterUpload: Boolean = false
    private var uploadedRoundAfterUpload: Int? = null

    // collected per-cycle parameter files (for deferred aggregated upload)
    @Suppress("unused")
    private val collectedParamFiles: MutableList<File> = mutableListOf()

    // Metrics and battery/CPU/memory/network/WebSocket sampling for recent training
    private data class EpochMetric(val epoch: Int, val durationMs: Long, val valLoss: Double?, val valAcc: Double?)
    private var epochMetrics: MutableList<EpochMetric> = mutableListOf()
    private var batteryLevelBeforeTraining: Int? = null
    private var batteryLevelAfterTraining: Int? = null

    // CPU/memory snapshots (millis / bytes)
    private var lastTrainingCpuMsUsed: Long? = null
    private var lastHeapBeforeBytes: Long? = null
    private var lastHeapAfterBytes: Long? = null
    private var lastNativeHeapBeforeBytes: Long? = null
    private var lastNativeHeapAfterBytes: Long? = null

    // upload related metrics
    private var lastUploadPreDelayMs: Long? = null
    private var lastUploadPostMs: Long? = null
    private var lastUploadedWeightsSha256: String? = null
    private var lastNetworkTransport: String? = null

    // WebSocket events history (timestamp -> event)
    private val websocketEvents: MutableList<Pair<Long, String>> = mutableListOf()

    // --- Experiment session tracking ---
    // A new session ID is generated at the start of each runFiveCycles() call.
    // This ties together satisfaction_before, all training cycles, and satisfaction_after.
    private var currentSessionId: String = ""
    private var sessionSatisfactionBefore: Float? = null
    private var sessionSatisfactionAfter: Float? = null

    private val _satisfactionBefore = MutableStateFlow<Float?>(null)
    val satisfactionBefore: StateFlow<Float?> = _satisfactionBefore.asStateFlow()

    private val _satisfactionAfter = MutableStateFlow<Float?>(null)
    val satisfactionAfter: StateFlow<Float?> = _satisfactionAfter.asStateFlow()

    // SP3d: After の 5 回計測（全値・標準偏差・件数）と、計測中の進み具合
    private var sessionAfterSummary: AfterSummary? = null
    private val _afterProgress = MutableStateFlow<Pair<Int, Int>?>(null)
    /** 計測中は (i, AFTER_SAMPLES)、それ以外は null。 */
    val afterProgress: StateFlow<Pair<Int, Int>?> = _afterProgress.asStateFlow()
    private val _isMeasuringAfter = MutableStateFlow(false)
    val isMeasuringAfter: StateFlow<Boolean> = _isMeasuringAfter.asStateFlow()
    private val _afterN = MutableStateFlow<Int?>(null)
    /** 直近の After の有効件数（未計測は null）。 */
    val afterN: StateFlow<Int?> = _afterN.asStateFlow()
    private val afterMeasuring = java.util.concurrent.atomic.AtomicBoolean(false)

    @Suppress("unused")
    fun recordWebSocketEvent(event: String) {
        try {
            val ts = System.currentTimeMillis()
            websocketEvents.add(ts to event)
            RealTimeLogger.i(TAG, "WebSocketEvent: ts=${ts} event=${event}")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "recordWebSocketEvent failed: ${e.message}")
        }
    }

    private fun readBatteryLevelPercent(): Int? {
        return try {
            val bm = getApplication<Application>().getSystemService(BatteryManager::class.java)
            val pct = bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
            pct?.coerceIn(0, 100)
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "readBatteryLevelPercent failed: ${e.message}")
            null
        }
    }

    private fun getNetworkTransport(): String? {
        return try {
            val cm = getApplication<Application>().getSystemService(Context.CONNECTIVITY_SERVICE) as? android.net.ConnectivityManager
            val net = cm?.activeNetwork ?: return null
            val caps = cm.getNetworkCapabilities(net) ?: return null
            return when {
                caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "WIFI"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "CELLULAR"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ETHERNET"
                else -> "OTHER"
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "getNetworkTransport failed: ${e.message}")
            null
        }
    }

    private fun sha256HexFromFloatArray(arr: FloatArray): String {
        return try {
            val bb = java.nio.ByteBuffer.allocate(arr.size * 4).order(java.nio.ByteOrder.LITTLE_ENDIAN)
            for (f in arr) bb.putFloat(f)
            val bytes = bb.array()
            val md = java.security.MessageDigest.getInstance("SHA-256")
            val digest = md.digest(bytes)
            digest.joinToString("") { "%02x".format(it) }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "sha256HexFromFloatArray failed: ${e.message}")
            ""
        }
    }

    private suspend fun persistTrainingMetrics(nSamples: Int) {
        withContext(Dispatchers.IO) {
            try {
                val app = getApplication<Application>()
                val dir = (app.getExternalFilesDir("logs") ?: app.filesDir)
                if (!dir.exists()) dir.mkdirs()
                val ts = System.currentTimeMillis()
                val csvFile = File(dir, "training_metrics_${ts}.csv")
                val summaryFile = File(dir, "training_summary_${ts}.json")

                csvFile.printWriter().use { out ->
                    out.println("epoch,duration_ms,val_loss,val_acc")
                    for (m in epochMetrics) {
                        out.println("${m.epoch},${m.durationMs},${m.valLoss ?: ""},${m.valAcc ?: ""}")
                    }
                }

                val totalMs = lastLocalTrainingTotalMs
                val avgEpochMs = if (epochMetrics.isNotEmpty()) epochMetrics.map { it.durationMs }.average() else 0.0
                val avgValLoss = epochMetrics.mapNotNull { it.valLoss }.let { if (it.isNotEmpty()) it.average() else null }
                val avgValAcc = epochMetrics.mapNotNull { it.valAcc }.let { if (it.isNotEmpty()) it.average() else null }

                val summary = JSONObject()
                summary.put("timestamp_ms", ts)
                summary.put("session_id", currentSessionId.ifEmpty { "no_session" })
                summary.put("assigned_app_index", _assignedAppIndex.value ?: 0)
                summary.put("assigned_app_type", appConfigs?.getOrNull(_assignedAppIndex.value ?: 0)?.appType ?: "unknown")
                summary.put("satisfaction_before", sessionSatisfactionBefore ?: JSONObject.NULL)
                summary.put("satisfaction_after", sessionSatisfactionAfter ?: JSONObject.NULL)
                putAfterSummary(summary, sessionAfterSummary)
                summary.put("n_samples", nSamples)
                summary.put("total_training_ms", totalMs)
                summary.put("avg_epoch_ms", avgEpochMs)
                if (avgValLoss != null) summary.put("avg_val_loss", avgValLoss)
                if (avgValAcc != null) summary.put("avg_val_acc", avgValAcc)
                summary.put("battery_before_pct", batteryLevelBeforeTraining ?: JSONObject.NULL)
                summary.put("battery_after_pct", batteryLevelAfterTraining ?: JSONObject.NULL)
                if (batteryLevelBeforeTraining != null && batteryLevelAfterTraining != null) {
                    summary.put("battery_delta_pct", (batteryLevelBeforeTraining!! - batteryLevelAfterTraining!!))
                }

                summary.put("training_cpu_ms", lastTrainingCpuMsUsed ?: JSONObject.NULL)
                summary.put("heap_before_bytes", lastHeapBeforeBytes ?: JSONObject.NULL)
                summary.put("heap_after_bytes", lastHeapAfterBytes ?: JSONObject.NULL)
                summary.put("native_heap_before_bytes", lastNativeHeapBeforeBytes ?: JSONObject.NULL)
                summary.put("native_heap_after_bytes", lastNativeHeapAfterBytes ?: JSONObject.NULL)

                summary.put("pre_upload_delay_ms", lastUploadPreDelayMs ?: JSONObject.NULL)
                summary.put("upload_post_ms", lastUploadPostMs ?: JSONObject.NULL)
                summary.put("uploaded_weights_sha256", lastUploadedWeightsSha256 ?: JSONObject.NULL)
                summary.put("network_transport", lastNetworkTransport ?: JSONObject.NULL)

                val wsArr = JSONArray()
                for ((t, e) in websocketEvents) {
                    val o = JSONObject()
                    o.put("ts", t)
                    o.put("event", e)
                    wsArr.put(o)
                }
                summary.put("websocket_events", wsArr)

                summaryFile.writeText(summary.toString())
                RealTimeLogger.i(TAG, "persistTrainingMetrics: wrote csv=${csvFile.absolutePath} summary=${summaryFile.absolutePath}")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "persistTrainingMetrics failed: ${e.message}")
            }
        }
    }

    /** このサイクルの学習側の値で行を作る（data_id を採番）。アップロード後の値は completeCycleMetric で補完する。 */
    private fun buildCycleRow(): JSONObject {
        val app = getApplication<Application>()
        val s = summarizeEpochs(epochMetrics.map { it.valAcc }, epochMetrics.map { it.valLoss })
        val appIdx = _assignedAppIndex.value ?: 0
        val appType = resolveAppType(appConfigs?.getOrNull(appIdx)?.appType)
        if (appType == "unknown") {
            try { RealTimeLogger.anomaly("app_type_unresolved", "app_index=$appIdx") } catch (_: Exception) {}
        }
        val row = TrainingMetricsStore.TrainingMetric(
            terminalId = AppConfig.getTerminalId(app),
            timestamp = System.currentTimeMillis(),
            totalLocalMs = lastLocalTrainingTotalMs,
            epochDurations = epochMetrics.map { it.durationMs },
            inputSize = modelInputSize.takeIf { it > 0 },
            hiddenSize = modelHiddenSize.takeIf { it > 0 },
            outputSize = modelOutputSize.takeIf { it > 0 },
            round = lastKnownRound,
            accuracy = s.lastAcc,
            loss = s.lastLoss,
            runId = com.example.hfl_experiment.experiment.ExperimentContext.runId.ifEmpty { null },
            experimentGroup = com.example.hfl_experiment.experiment.ExperimentContext.experimentGroup.ifEmpty { null },
            cycleNumber = _currentCycle.value.takeIf { it > 0 },
            epochCount = epochMetrics.size.takeIf { it > 0 },
            valAccuracy = s.lastAcc,
            valLoss = s.lastLoss,
            dataSamplesCount = fullDataset.size.takeIf { it > 0 },
            batteryAtStart = batteryLevelBeforeTraining?.toDouble(),
            batteryAtEnd = batteryLevelAfterTraining?.toDouble(),
            memUsedMbAtStart = lastHeapBeforeBytes?.let { it / 1024.0 / 1024.0 },
            memUsedMbAtEnd = lastHeapAfterBytes?.let { it / 1024.0 / 1024.0 },
            satisfactionBefore = sessionSatisfactionBefore?.toDouble(),
            appType = appType,
            appIndex = appIdx,
            modelVersion = com.example.hfl_experiment.experiment.ExperimentContext.modelVersion.ifEmpty { null },
            valAccuracyMean = s.meanAcc,
            valLossMean = s.meanLoss,
            epochValAccuracy = s.accs,
            epochValLoss = s.losses,
            virtualRouterId = currentRouterId
        ).toJson()
        row.put("baseline_mode", AppConfig.BASELINE_MODE)
        row.put("baseline_mode_source", AppConfig.baselineModeSource.value)
        row.put("hfl_mode", AppConfig.hflMode)
        row.put("hfl_mode_source", AppConfig.hflModeSource.value)
        row.put("init_source", lastInitSource)
        putDataPartition(row, dataPartition)  // E8
        putTrainingData(row, trainingDataInfo)  // rebuild-training-data
        putConnectedAp(row, readConnectedAp())  // record-connected-ap
        row.put("tp_unit", com.example.hfl_experiment.util.networking.NetworkBandwidthProbe.TP_UNIT)  // E7: 旧データ（MB/s）と区別
        row.put("init_model_sha256", lastInitModelSha ?: JSONObject.NULL)
        row.put("data_id", java.util.UUID.randomUUID().toString())
        return withSessionSettings(row, AppConfig.numCycles, AppConfig.numCyclesSource.value)
    }

    /** SP3b R1: 端末設定とサーバの HFL モードの食い違いを実験イベント（hfl_mode_mismatch）として残す。 */
    private fun recordHflModeMismatch(app: String, server: String) {
        val a = getApplication<Application>()
        try {
            val ev = JSONObject().put("kind", "experiment_event").put("type", "hfl_mode_mismatch")
                .put("terminal_id", terminalId).put("app", app).put("server", server)
                .put("run_id", currentSessionId).put("timestamp_ms", System.currentTimeMillis())
            try { RealTimeLogger.logEvent("hfl_mode_mismatch", mapOf("app" to app, "server" to server, "run_id" to currentSessionId)) } catch (_: Exception) {}
            TrainingMetricsStore.init(a)
            TrainingMetricsStore.recordRow(ev)
            com.example.hfl_experiment.worker.UploadMetricsWorker.enqueue(a)
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "recordHflModeMismatch failed: ${e.message}")
        }
    }

    /** 台帳へ記録し、未送信キュー経由で自動送信する。 */
    private fun recordAndSend(row: JSONObject) {
        val app = getApplication<Application>()
        try {
            TrainingMetricsStore.init(app)
            TrainingMetricsStore.recordRow(row)
            com.example.hfl_experiment.worker.UploadMetricsWorker.enqueue(app)
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "recordAndSend failed: ${e.message}")
        }
    }

    private var currentCycleRow: JSONObject? = null

    private var lastEpochDurationsMs: List<Long> = emptyList()
    private var lastLocalTrainingTotalMs: Long = 0L
    private var lastBandwidthMbps: Double? = null
    private var lastMeasuredRttMs: Float? = null
    private var lastDecisionResult: com.example.hfl_experiment.experiment.DecisionResult? = null
    /** hfl_rec のセッションの（問い合わせの状態, 推奨 AP）。After の補完で hfl_rec_followed を求める。hfl_rec 以外は null。 */
    private var hflRecSession: Pair<String, Int?>? = null

    private var currentRunJob: Job? = null

    // SP5: adb の auto_start で始めたセッション（無人モード）。run_tag は session_complete に入れて返す
    private val autoStartPending = java.util.concurrent.atomic.AtomicBoolean(false)
    @Volatile private var unattended = false
    @Volatile private var unattendedRunTag: String? = null
    @Volatile private var sessionStartedAtMs: Long = 0L
    // runFiveCycles の終わりに控えた完了イベントの元（無人モードでは After の後に送る）
    @Volatile private var pendingSessionComplete: SessionCompleteInfo? = null

    /**
     * SP5: ボタン（Fetch Training Data → Start N Cycles）と同じ処理を順に行う。実行中なら何もせず理由をログに残す。
     * numCycles はアプリ設定に保存する（サーバの指定が優先されるのは SP3d のまま）。
     */
    fun autoStartSession(runTag: String?, numCycles: Int?, appConfigsSize: Int?) {
        val d = decideAutoStart(isRunning = _isTrainingRunning.value || autoStartPending.get(),
            trainingFinished = _trainingFinished.value)
        if (d is AutoStartDecision.Ignore || !autoStartPending.compareAndSet(false, true)) {
            val reason = (d as? AutoStartDecision.Ignore)?.reason ?: "already_running"
            RealTimeLogger.w(TAG, "auto_start ignored: $reason run_tag=$runTag")
            try { RealTimeLogger.logEvent("auto_start_ignored", mapOf("reason" to reason, "run_tag" to runTag)) } catch (_: Exception) {}
            return
        }
        viewModelScope.launch {
            try {
                unattended = true
                unattendedRunTag = runTag
                RealTimeLogger.i(TAG, "auto_start: run_tag=$runTag num_cycles=$numCycles")
                if (numCycles != null && AppConfig.isValidNumCycles(numCycles)) {
                    AppConfig.setNumCycles(getApplication(), numCycles)
                }
                fetchAndPrepareTrainingData(null).join()
                if (!isTrainingDataReady.value) {
                    RealTimeLogger.w(TAG, "auto_start: training data not ready (fetch_failed)")
                    onAutoStartFetchFailed(runTag)
                    return@launch
                }
                val a = buildRunArgs(appConfigsSize)
                pendingSessionComplete = null
                try {
                    runFiveCycles(inputSize = a.inputSize, hiddenSize = a.hiddenSize, outputSize = a.outputSize,
                        epochs = a.epochs, autoUpload = a.autoUpload)
                } finally {
                    finishUnattendedSession(runTag)
                }
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "auto_start failed: ${e.message}")
            } finally {
                unattended = false
                unattendedRunTag = null
                autoStartPending.set(false)
            }
        }
    }

    /** データの準備に失敗した無人セッション: セッションは始まっていないが、ランナーが待たずに済むよう error で知らせる。 */
    private fun onAutoStartFetchFailed(runTag: String?) {
        val now = System.currentTimeMillis()
        sendSessionComplete(sessionInfoNow(runTag, result = "error", reason = "fetch_failed", startedAtMs = now)
            .copy(afterSwitchConfirmed = "unknown", endedAtMs = now))
    }

    /**
     * SP5: 無人セッションの後始末。正常に終わったら AP の切り替えを待って（最大 AFTER_SWITCH_WAIT_MS）After を自動で測り、
     * その後に session_complete を送る。runFiveCycles が始まらなかった（拒否された）ときは error で送る。
     */
    private suspend fun finishUnattendedSession(runTag: String?) {
        val info = pendingSessionComplete
        pendingSessionComplete = null
        if (info == null) {
            val now = System.currentTimeMillis()
            sendSessionComplete(sessionInfoNow(runTag, result = "error", reason = "start_refused", startedAtMs = now)
                .copy(afterSwitchConfirmed = "unknown", endedAtMs = now))
            return
        }
        var status = "unknown"
        if (info.result == "ok") {
            try {
                status = awaitSwitchTarget()
                measureSatisfactionAfter(afterSwitchConfirmed = status)
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "unattended After failed: ${e.message}")
            }
        }
        sendSessionComplete(info.copy(afterSwitchConfirmed = status, endedAtMs = System.currentTimeMillis()))
    }

    /** 最後に提案した AP（SwitchTarget）に繋がるまで待つ。提案が無ければすぐ "true"。 */
    private suspend fun awaitSwitchTarget(): String {
        val target = SwitchTarget.ssid
        val app = getApplication<Application>()
        // ap-detection-fallback: 提案の AP 番号と、いまの AP 番号（SSID またはサブネットで判定）を比べる。SSID が隠される端末でも切替を確認できる
        val targetAp = apIndexOfSsid(normalizeSsid(target), AppConfig.getPrimarySsid(app), AppConfig.getSecondarySsid(app))
        if (target.isNullOrBlank()) return afterSwitchStatusByAp(readConnectedAp(), null, timedOut = false)
        if (targetAp == null) {  // 設定にない SSID への提案は、従来どおり SSID の一致で見る
            val ssidNow = { try { com.example.hfl_experiment.util.networking.NetworkSwitchHelper.getConnectedSsid(app) } catch (_: Exception) { null } }
            val end0 = System.currentTimeMillis() + AFTER_SWITCH_WAIT_MS
            while (System.currentTimeMillis() < end0) {
                if (ssidNow() == target) return "true"
                kotlinx.coroutines.delay(AFTER_SWITCH_POLL_MS)
            }
            return afterSwitchStatus(ssidNow(), target, timedOut = true)
        }
        val end = System.currentTimeMillis() + AFTER_SWITCH_WAIT_MS
        while (System.currentTimeMillis() < end) {
            if (readConnectedAp().ap == targetAp) return "true"
            kotlinx.coroutines.delay(AFTER_SWITCH_POLL_MS)
        }
        val st = afterSwitchStatusByAp(readConnectedAp(), targetAp, timedOut = true)
        RealTimeLogger.w(TAG, "unattended After: AP switch not confirmed target=$target status=$st")
        return st
    }

    /** いまのセッション設定から完了イベントの元を作る（runFiveCycles の終わり、endSession* の前に呼ぶ）。 */
    private fun sessionInfoNow(runTag: String?, result: String, reason: String?, startedAtMs: Long): SessionCompleteInfo {
        val runId = currentSessionId
        val cycles = try {
            TrainingMetricsStore.init(getApplication())
            TrainingMetricsStore.latestRows().values.count {
                it.optString("run_id") == runId && runId.isNotEmpty() && it.optString("kind") != "experiment_event" &&
                    it.optString("row_kind") != "baseline_session"
            }
        } catch (_: Exception) { 0 }
        return SessionCompleteInfo(
            terminalId = terminalId, runId = runId, runTag = runTag, result = result, reason = reason,
            hflMode = AppConfig.hflMode, hflModeSource = AppConfig.hflModeSource.value,
            numCycles = AppConfig.numCycles, numCyclesSource = AppConfig.numCyclesSource.value,
            baselineMode = AppConfig.BASELINE_MODE, cyclesCompleted = cycles, afterSwitchConfirmed = "not_measured",
            startedAtMs = startedAtMs, endedAtMs = System.currentTimeMillis())
    }

    /** session_complete を台帳と未送信キューに入れて送る（SP-M の再送の仕組み）。 */
    private fun sendSessionComplete(info: SessionCompleteInfo) {
        val a = getApplication<Application>()
        try {
            val ev = buildSessionCompleteEvent(info)
            try { RealTimeLogger.logEvent("session_complete", mapOf("run_tag" to info.runTag, "result" to info.result,
                "reason" to info.reason, "after_switch_confirmed" to info.afterSwitchConfirmed)) } catch (_: Exception) {}
            TrainingMetricsStore.init(a)
            TrainingMetricsStore.recordRow(ev)
            com.example.hfl_experiment.worker.UploadMetricsWorker.enqueue(a)
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "sendSessionComplete failed: ${e.message}")
        }
    }
    // アップロードは常に1本。再開時は旧ジョブの完全終了を待つ（ReplaceableJob 参照）
    private val uploadSlot = ReplaceableJob()

    // SP1: 前サイクルの自モデルと、最後に学習に使ったダウンロード済みモデルの SHA-256
    private var lastTrainedParams: ModelParameters? = null
    private var lastUsedDownloadedSha: String? = null
    private var lastInitSource: String = "unknown"
    private var lastInitModelSha: String? = null

    private fun downloadedModelSha(): String? {
        val f = LocalTrainer.downloadedModelFile(getApplication<Application>().filesDir)
        return if (f.isFile) WeightBinLoader.sha256Hex(f) else null
    }

    /** 学習器生成後に呼ぶ: 初期値の出所を記録し、ダウンロード済みモデルを（読込失敗を含め）使った場合はその SHA を覚える。 */
    private fun recordTrainingInit(trainer: LocalTrainer?, sha: String?) {
        val src = trainer?.initSource ?: "unknown"
        lastUsedDownloadedSha = usedDownloadedSha(src, sha, lastUsedDownloadedSha)
        lastInitSource = src
        lastInitModelSha = if (src == "DOWNLOADED") sha else null
        RealTimeLogger.i(TAG, "training_init source=$src model_sha256=${lastInitModelSha ?: "-"} cycle=${_currentCycle.value}")
    }

    /** 最新の配布モデルを取得・適用する（メタポーリングとサイクル間で共用）。取得できなければ false（自モデルで継続）。 */
    private suspend fun fetchAndApplyLatestGlobalModel(reason: String): Boolean = withContext(Dispatchers.IO) {
        val dto = try { networkClient.fetchSendToDevice() } catch (e: Exception) {
            RealTimeLogger.w(TAG, "fetchSendToDevice failed ($reason): ${e.message}"); null
        }
        val modelLink = dto?.links?.model ?: dto?.paths?.model_rel ?: dto?.model?.path
        if (modelLink.isNullOrBlank()) {
            RealTimeLogger.i(TAG, "global_model_fetch reason=$reason: no model link; will rely on broadcast/polling")
            return@withContext false
        }
        val dlClient = okhttp3.OkHttpClient.Builder()
            .connectTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
            .readTimeout(120, java.util.concurrent.TimeUnit.SECONDS)
            .writeTimeout(120, java.util.concurrent.TimeUnit.SECONDS)
            .build()
        val appCtx = getApplication<Application>()
        val (ok, msg) = try {
            ModelUpdateUtil.applyModelLink(appCtx, edgeBaseUrl, modelLink, dlClient, AppConfig.getServerAuthToken(appCtx))
        } catch (e: Exception) {
            Pair(false, e.message ?: "error")
        }
        RealTimeLogger.i(TAG, "global_model_fetch reason=$reason ok=$ok msg=$msg link=$modelLink")
        ok
    }

    // Make channel buffered to reduce risk of missed notifications; capacity 16 is reasonable for this use-case
    private val modelUpdateChannel: Channel<String?> = Channel(capacity = 16)

    private val metaPollingInFlight = AtomicBoolean(false)
    private var metaPollingJob: Job? = null

    private val recentMetaRequests = ArrayDeque<Long>()
    private var metaBackoffMs: Long = 0L
    private val RATE_SHORT_WINDOW_MS: Long = 1000L
    private val RATE_MAX_REQUESTS_PER_WINDOW: Int = 3
    private val RATE_BACKOFF_INITIAL_MS: Long = 1000L
    private val RATE_BACKOFF_MAX_MS: Long = 30_000L

    private val _currentCycle = MutableStateFlow(0)
    val currentCycle: StateFlow<Int> = _currentCycle

    private val _totalCycles = MutableStateFlow(AppConfig.appNumCycles)
    val totalCycles: StateFlow<Int> = _totalCycles

    private val _currentEpoch = MutableStateFlow(0)
    val currentEpoch: StateFlow<Int> = _currentEpoch

    private val _epochsPerCycle = MutableStateFlow(1)
    val epochsPerCycle: StateFlow<Int> = _epochsPerCycle

    // Additional UI hooks used by MainActivity/Compose — lightweight stubs to satisfy callers
    private val _networkPanelRequested = MutableStateFlow<String?>(null)
    val networkPanelRequested: StateFlow<String?> = _networkPanelRequested

    private val _networkSuggestionMsg = MutableStateFlow<String?>(null)
    val networkSuggestionMsg: StateFlow<String?> = _networkSuggestionMsg

    private val _logUploadStatus = MutableStateFlow<String?>(null)
    val logUploadStatus: StateFlow<String?> = _logUploadStatus

    private val _networkForcedEvent = MutableStateFlow<String?>(null)
    val networkForcedEvent: StateFlow<String?> = _networkForcedEvent

    private val _isLogUploading = MutableStateFlow(false)
    val isLogUploading: StateFlow<Boolean> = _isLogUploading

    // NEW: Terminal satisfaction exposed to UI (nullable until measured)
    private val _terminalSatisfaction = MutableStateFlow<Float?>(null)
    val terminalSatisfaction: StateFlow<Float?> = _terminalSatisfaction.asStateFlow()

    // NEW: SharedFlow to notify UI when inference/round update is received
    private val _inferenceEvent = MutableSharedFlow<String>(extraBufferCapacity = 4)
    val inferenceEvent = _inferenceEvent.asSharedFlow()

    // NEW: Current AP index observable (0/1)
    private val _currentApIndex = MutableStateFlow(0)
    val currentApIndex: StateFlow<Int> = _currentApIndex.asStateFlow()

    // NEW: Server global round observable
    private val _serverRound = MutableStateFlow<Int?>(null)
    val serverRound: StateFlow<Int?> = _serverRound.asStateFlow()

    // Called by UI when server indicates a new round (WebSocket/Ask flow)
    fun onNewRoundReceived(round: Int) {
        try {
            val prev = lastKnownRound ?: -1
            lastKnownRound = round
            persistLastKnownRound(round)
            // update observable server round and notify UI listeners
            try { _serverRound.value = round } catch (_: Exception) {}
            try { viewModelScope.launch { _inferenceEvent.emit("${prev} -> ${round}") } } catch (_: Exception) {}
            _trainingDataStatus.value = "外部で新しいラウンドが適用されました (round=${round})"
            _errorMessage.value = null
            RealTimeLogger.i(TAG, "onNewRoundReceived: round=${round} (emitted inference event ${prev} -> ${round})")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "onNewRoundReceived failed: ${e.message}")
        }
    }

    fun clearNetworkForcedEvent() {
        try { _networkForcedEvent.value = null } catch (_: Exception) {}
    }

    fun clearLogUploadStatus() {
        try { _logUploadStatus.value = null } catch (_: Exception) {}
    }

    // Best-effort placeholder: measure satisfaction (returns null if not implemented)
    // label: "before" or "after" — used to name the output file and tag history entries.
    suspend fun measureAndLogTerminalSatisfaction(selectedApps: List<Int>? = null, label: String? = null): Float? {
        try {
            // Determine which app indices to evaluate. Prefer caller-provided list; otherwise use the
            // device's one-time assigned app index so the app type remains fixed across the lifecycle.
            val appNumMax = if (appConfigs.isNullOrEmpty()) AppConfig.APP_CAT_COUNT else appConfigs!!.size
            ensureAssignedAppInitialized(appNumMax)
            val assigned = _assignedAppIndex.value ?: 0
            val appsToCheck = try {
                if (!selectedApps.isNullOrEmpty()) selectedApps else listOf(assigned)
            } catch (_: Exception) { listOf(assigned) }

            // Compose probe URL for bandwidth measurement
            val probeUrl = try { AppConfig.getEdgeBaseUrl(getApplication()).trimEnd('/') + "/static/probe.bin" } catch (_: Exception) { "" }

            // Measure server RTT (OkHttp HEAD) and bandwidth (small GET)
            lastMeasuredRttMs = try { withContext(Dispatchers.IO) { networkClient.measureRtt(null, attempts = 3, perAttemptTimeoutMs = 3000) } } catch (e: Exception) { RealTimeLogger.w(TAG, "measureAndLogTerminalSatisfaction: measureRtt failed: ${e.message}"); null }
            // normalize to Double? for downstream computations
            val rttVal: Double? = try {
                if (lastMeasuredRttMs != null) lastMeasuredRttMs!!.toDouble() else com.example.hfl_experiment.util.networking.PingUtil.pingGateway(getApplication())?.toDouble()
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "measureAndLogTerminalSatisfaction: pingGateway failed: ${e.message}")
                null
            }

            lastBandwidthMbps = try {
                withContext(Dispatchers.IO) {
                    val ok = okhttp3.OkHttpClient.Builder().build()
                    com.example.hfl_experiment.util.networking.NetworkBandwidthProbe.measureMbps(ok, probeUrl)
                }
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "measureAndLogTerminalSatisfaction: bandwidth probe failed: ${e.message}")
                null
            }

            // For each app, compute satisfaction and average — only include apps with actual measurements
            val scores = mutableListOf<Double>()
            val appDetails = mutableListOf<org.json.JSONObject>()
             for (idx in appsToCheck) {
                 val cfg = appConfigs?.getOrNull(idx)
                 val indicator = cfg?.indicator?.uppercase() ?: "RTT"
                 val needRtt = cfg?.needRTT?.toDouble() ?: 100.0
                 val needTp = cfg?.needTP?.toDouble() ?: 5.0
                 try {
                     if (indicator == "RTT") {
                         if (rttVal == null) {
                             RealTimeLogger.w(TAG, "measureAndLogTerminalSatisfaction: skipping idx=$idx RTT measurement missing")
                             continue
                         }
                         val measured = rttVal
                        val rawScore = if (needRtt <= 0.0) 1.0 else (needRtt / measured)
                        val s = com.example.hfl_experiment.util.networking.SatisfactionUtil.compute(
                            com.example.hfl_experiment.util.networking.SatisfactionUtil.AppGroup.G_RTT,
                            measured,
                            needRtt
                        )
                         scores.add(s)
                         // record per-app detail for diagnostics
                         try {
                             val o = org.json.JSONObject()
                             o.put("idx", idx)
                             o.put("app_type", cfg?.appType ?: "unknown")
                             o.put("indicator", "RTT")
                             o.put("need_rtt", needRtt)
                             o.put("measured_rtt", measured)
                             o.put("raw_score", rawScore)
                             o.put("score", s)
                             appDetails.add(o)
                             RealTimeLogger.i(TAG, "Satisfaction[app=$idx RTT]: measured=${measured} need=${needRtt} score=${s}")
                         } catch (_: Exception) {}
                     } else {
                         if (lastBandwidthMbps == null) {
                             RealTimeLogger.w(TAG, "measureAndLogTerminalSatisfaction: skipping idx=$idx TP measurement missing")
                             continue
                         }
                         val measuredTp = lastBandwidthMbps
                        val rawScore = if (needTp <= 0.0) 1.0 else (measuredTp!! / needTp)
                        val s = com.example.hfl_experiment.util.networking.SatisfactionUtil.compute(
                            com.example.hfl_experiment.util.networking.SatisfactionUtil.AppGroup.G_TP,
                            measuredTp!!,
                            needTp
                        )
                         scores.add(s)
                         try {
                             val o = org.json.JSONObject()
                             o.put("idx", idx)
                             o.put("app_type", cfg?.appType ?: "unknown")
                             o.put("indicator", "TP")
                             o.put("need_tp", needTp)
                             o.put("measured_tp_mbps", measuredTp)
                             o.put("raw_score", rawScore)
                             o.put("score", s)
                             appDetails.add(o)
                             RealTimeLogger.i(TAG, "Satisfaction[app=$idx TP]: measured=${measuredTp} need=${needTp} score=${s}")
                         } catch (_: Exception) {}
                     }
                 } catch (e: Exception) {
                     RealTimeLogger.w(TAG, "compute satisfaction failed for idx=$idx: ${e.message}")
                 }
             }

             val avg = if (scores.isNotEmpty()) scores.average().toFloat() else null
             postApObservation(lastBandwidthMbps, lastMeasuredRttMs?.toDouble())  // record-connected-ap

             // Persist satisfaction data and update UI state
             try {
                 val ts = System.currentTimeMillis()
                 val jo = org.json.JSONObject()
                 jo.put("timestamp_ms", ts)
                 jo.put("session_id", currentSessionId.ifEmpty { "no_session" })
                 jo.put("label", label ?: "measure")
                 jo.put("assigned_app_index", appsToCheck.firstOrNull() ?: assigned)
                 jo.put("assigned_app_type", appConfigs?.getOrNull(appsToCheck.firstOrNull() ?: assigned)?.appType ?: "unknown")
                 jo.put("satisfaction_avg", avg ?: org.json.JSONObject.NULL)
                 jo.putOpt("server_rtt_ms", lastMeasuredRttMs)
                 jo.putOpt("gateway_rtt_ms", rttVal)
                 jo.putOpt("bandwidth_mbps", lastBandwidthMbps)
                 try {
                     val arr = org.json.JSONArray()
                     for (o in appDetails) arr.put(o)
                     jo.put("app_scores", arr)
                 } catch (_: Exception) {}

                 val filesDir = getApplication<Application>().filesDir

                 // 1. Always overwrite the "latest" file for backward compatibility
                 try {
                     File(filesDir, "terminal_satisfaction.json").writeText(jo.toString())
                 } catch (e: Exception) {
                     RealTimeLogger.w(TAG, "measureAndLogTerminalSatisfaction: failed to write terminal_satisfaction.json: ${e.message}")
                 }

                 // 2. Write a session-labelled file so before/after are never overwritten
                 try {
                     val suffix = if (currentSessionId.isNotEmpty() && label != null) {
                         "${currentSessionId}_${label}"
                     } else {
                         ts.toString()
                     }
                     File(filesDir, "terminal_satisfaction_${suffix}.json").writeText(jo.toString())
                 } catch (e: Exception) {
                     RealTimeLogger.w(TAG, "measureAndLogTerminalSatisfaction: failed to write timestamped file: ${e.message}")
                 }

                 // 3. Append to JSONL history — one line per measurement, never overwritten
                 try {
                     File(filesDir, "satisfaction_history.jsonl").appendText(jo.toString() + "\n")
                 } catch (e: Exception) {
                     RealTimeLogger.w(TAG, "measureAndLogTerminalSatisfaction: failed to append satisfaction_history.jsonl: ${e.message}")
                 }

                 RealTimeLogger.i(TAG, "measureAndLogTerminalSatisfaction: avg=${avg} label=${label} session=${currentSessionId}")
                 try { _terminalSatisfaction.value = avg } catch (_: Exception) {}
             } catch (e: Exception) {
                 RealTimeLogger.w(TAG, "measureAndLogTerminalSatisfaction: failed to persist satisfaction: ${e.message}")
             }

             try {
                 val de = decisionEngine
                 val tp = lastBandwidthMbps?.toFloat() ?: 0f
                 val rtt = lastMeasuredRttMs ?: 0f
                 val appIdx = _assignedAppIndex.value ?: 0
                 val appNum = appConfigs?.size?.coerceAtLeast(4) ?: 4
                 val oneHot = FloatArray(appNum) { i -> if (i == appIdx) 1f else 0f }
                 if (de != null) {
                     val result = de.observe(tp, rtt, oneHot)
                     lastDecisionResult = result
                     if (AppConfig.BASELINE_MODE == "greedy") {
                         for ((apIdx, src) in de.lastGreedySources) greedySourceCounts.merge("AP$apIdx:$src", 1) { a, b -> a + b }
                     }
                     RealTimeLogger.i(TAG, "DecisionEngine.observe: chosenAP=${result.chosenApId} reason=${result.reason} ema=${result.emaScores}")
                 }
             } catch (e: Exception) {
                 RealTimeLogger.w(TAG, "DecisionEngine.observe failed: ${e.message}")
             }

             return avg
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "measureAndLogTerminalSatisfaction failed: ${e.message}")
            return null
        }
    }

    /**
     * AP切り替え後に UIから呼ぶ満足度計測メソッド。
     * runFiveCycles() で生成された sessionId と紐づいた satisfaction_after として記録される。
     * SP3d: AFTER_SAMPLES 回（AFTER_INTERVAL_MS おき）測り、有効値の平均を satisfaction_after、全値・標準偏差・件数も残す。
     * 計測中の二重押しは無視する（null を返す）。戻り値は平均。
     */
    suspend fun measureSatisfactionAfter(afterSwitchConfirmed: String? = null): Float? {
        if (!afterMeasuring.compareAndSet(false, true)) {
            RealTimeLogger.w(TAG, "measureSatisfactionAfter: already measuring — ignored")
            return null
        }
        val summary: AfterSummary
        try {
            _isMeasuringAfter.value = true
            RealTimeLogger.i(TAG, "measureSatisfactionAfter: 手動計測開始 ($AFTER_SAMPLES 回, ${AFTER_INTERVAL_MS}ms おき)")
            val samples = mutableListOf<Double?>()
            // fix-audit-20261026 N2: 安定待ち（常に AFTER_SETTLE_MS）。待った後に開始時の AP を読む（record-connected-ap）
            withContext(Dispatchers.Main) { _trainingDataStatus.value = "After の前に ${AFTER_SETTLE_MS / 1000} 秒待っています（切替直後の影響を避ける）" }
            val (settled, apStart) = settleThenReadAp(AFTER_SETTLE_MS, { kotlinx.coroutines.delay(it) }) { readConnectedAp() }
            afterSettleMsUsed = settled
            apAtAfterStart = apStart
            for (i in 1..AFTER_SAMPLES) {
                _afterProgress.value = i to AFTER_SAMPLES
                // 1. 現在の品質を計測（label="after_i" で記録。各回のファイル・satisfaction_history.jsonl は従来どおり残る）
                val v = try { measureAndLogTerminalSatisfaction(null, label = "after_$i") } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "measureSatisfactionAfter: sample $i failed: ${e.message}"); null
                }
                samples.add(v?.toDouble())
                if (i < AFTER_SAMPLES) kotlinx.coroutines.delay(AFTER_INTERVAL_MS)
            }
            summary = summarizeAfterSamples(samples)
            apAtAfterEnd = readConnectedAp()  // 終了時の AP（違えば ap_changed_during_after）
        } finally {
            _afterProgress.value = null
            _isMeasuringAfter.value = false
            afterMeasuring.set(false)
        }
        val result = summary.mean?.toFloat()
        RealTimeLogger.i(TAG, "measureSatisfactionAfter: mean=${summary.mean} std=${summary.std} n=${summary.n} samples=${summary.samples}")
        sessionSatisfactionAfter = result
        sessionAfterSummary = summary
        try { _satisfactionAfter.value = result } catch (_: Exception) {}
        try { _afterN.value = summary.n } catch (_: Exception) {}

        // 2. 実験レコードを即座に更新保存
        if (currentSessionId.isNotEmpty()) {
            try { saveExperimentRecord() } catch (e: Exception) {
                RealTimeLogger.w(TAG, "measureSatisfactionAfter: saveExperimentRecord failed: ${e.message}")
            }
        }

        // 3. テレメトリ用の一時ファイルを強制的に書き出し
        try {
            val jot = org.json.JSONObject()
            jot.put("timestamp_ms", System.currentTimeMillis())
            jot.put("switched", true) // 手動で切り替えた前提なので true 固定
            if (sessionSatisfactionBefore != null) jot.put("satisfaction_before", sessionSatisfactionBefore!!.toDouble())
            if (result != null) jot.put("satisfaction_after", result.toDouble())
            
            val f = java.io.File(getApplication<Application>().filesDir, "terminal_satisfaction.json")
            f.writeText(jot.toString())
            RealTimeLogger.i(TAG, "measureSatisfactionAfter: terminal_satisfaction.json を強制更新しました")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "measureSatisfactionAfter: JSON保存失敗: ${e.message}")
        }

        // 4. 即座にサーバーへテレメトリを飛ばす (After の値を届ける)
        kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO).launch {
            try {
                val sent = com.example.hfl_experiment.telemetry.TelemetrySender.send(getApplication())
                RealTimeLogger.i(TAG, "measureSatisfactionAfter: テレメトリ即時送信結果=$sent")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "measureSatisfactionAfter: テレメトリ送信失敗: ${e.message}")
            }
        }

        // 5. セッション最終サイクルの行（同じ data_id）に After（平均・全値・標準偏差・件数）を補完して送る（SP-M, SP3d）。
        //    有効値が 0 個（result == null）なら補完しない
        if (result != null && currentSessionId.isNotEmpty()) {
            try {
                TrainingMetricsStore.init(getApplication())
                TrainingMetricsStore.lastCycleRow(currentSessionId)?.let { last ->
                    val row = completeAfterMetric(last, summary, System.currentTimeMillis())
                    putAfterAp(row, apAtAfterStart, apAtAfterEnd)  // record-connected-ap
                    putAfterSettle(row, afterSettleMsUsed)  // fix-audit-20261026 N2
                    hflRecSession?.let { (st, ap) ->   // hfl-recommended-ap-mode: 推奨に従ったか（After 終了時の AP と推奨の一致。不明は null）
                        row.put("hfl_rec_followed", com.example.hfl_experiment.experiment.hflRecFollowed(st, ap, apAtAfterEnd.ap) ?: JSONObject.NULL)
                    }
                    // SP5: 無人モードでは、After の時点で意図した AP に繋がっていたかを残す
                    afterSwitchConfirmed?.let { row.put("after_switch_confirmed", it) }
                    recordAndSend(row)
                } ?: RealTimeLogger.w(TAG, "measureSatisfactionAfter: no cycle row for run_id=$currentSessionId")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "measureSatisfactionAfter: metric update failed: ${e.message}")
            }
        }

        return result
    }

    /**
     * 学習中止後など、**学習完了を待たずに**エッジへ状況ログを送る（エラーレポート）。
     * `filesDir/logs/realtime_events.log` 等を `filesDir/log/error_report_*` にコピーしてから
     * [com.example.hfl_experiment.work.UploadLogsWorker.enqueueErrorReportOnce] で送信する。
     */
    fun uploadCurrentLogFile() {
        try {
            viewModelScope.launch(Dispatchers.IO) {
                try {
                    _isLogUploading.value = true
                    val app = getApplication<Application>()
                    prepareErrorReportSnapshots(app)
                    val n = countErrorReportSnapshotFiles(app)
                    if (n == 0) {
                        RealTimeLogger.w(TAG, "uploadCurrentLogFile: no snapshot files — enqueueing meta-only error report")
                    }
                    RealTimeLogger.logEvent(
                        "error_report_enqueue",
                        mapOf(
                            "snapshot_files" to n,
                            "meta_only" to (n == 0).toString(),
                            "training_running" to _isTrainingRunning.value.toString(),
                        )
                    )
                    com.example.hfl_experiment.work.UploadLogsWorker.enqueueErrorReportOnce(app)
                    _logUploadStatus.value = if (n == 0) {
                        "Upload queued: ログ本文はありませんが、端末メタのみのエラーレポートを送信キューに入れました（Wi‑Fi 接続後にアップロード）。"
                    } else {
                        "Upload successful: エラーレポートを送信キューに入れました（Wi‑Fi 接続後にアップロード）。ファイル数=$n"
                    }
                    com.example.hfl_experiment.work.UploadLogsWorker.lastStatus(app)?.takeIf { !it.optBoolean("ok", true) }?.let {
                        _logUploadStatus.value += "\n前回の送信は拒否されました（HTTP ${it.optInt("http_code")}: ${it.optString("message")}）。設定を確認してください。"
                    }
                } catch (e: Exception) {
                    _logUploadStatus.value = "failed: ${e.message ?: "unknown"}"
                    RealTimeLogger.w(TAG, "uploadCurrentLogFile failed: ${e.message}")
                } finally {
                    _isLogUploading.value = false
                }
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "uploadCurrentLogFile launch failed: ${e.message}")
        }
    }

    private fun prepareErrorReportSnapshots(app: Application) {
        val logDir = File(app.filesDir, "log").apply { mkdirs() }
        fun copyIfPositive(src: File, destName: String) {
            try {
                if (src.exists() && src.length() > 0L) {
                    src.copyTo(File(logDir, destName), overwrite = true)
                }
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "prepareErrorReportSnapshots: ${src.name} -> $destName failed: ${e.message}")
            }
        }
        copyIfPositive(File(app.filesDir, "logs/realtime_events.log"), com.example.hfl_experiment.work.UploadLogsWorker.ERROR_REPORT_REALTIME_NAME)
        copyIfPositive(File(app.filesDir, "model_update_log.txt"), com.example.hfl_experiment.work.UploadLogsWorker.ERROR_REPORT_MODEL_NAME)
        val trainCsv = File(logDir, "training_log.csv")
        copyIfPositive(trainCsv, com.example.hfl_experiment.work.UploadLogsWorker.ERROR_REPORT_TRAINING_NAME)
    }

    private fun countErrorReportSnapshotFiles(app: Application): Int {
        val logDir = File(app.filesDir, "log")
        var n = 0
        listOf(
            com.example.hfl_experiment.work.UploadLogsWorker.ERROR_REPORT_REALTIME_NAME,
            com.example.hfl_experiment.work.UploadLogsWorker.ERROR_REPORT_MODEL_NAME,
            com.example.hfl_experiment.work.UploadLogsWorker.ERROR_REPORT_TRAINING_NAME,
        ).forEach { name ->
            val f = File(logDir, name)
            if (f.isFile && f.length() > 0L) n++
        }
        return n
    }

    // Dump logs and return a boolean indicating if any files were exported (best-effort)
    fun dumpAndUploadLogs(): Boolean {
        try {
            viewModelScope.launch(Dispatchers.IO) {
                try {
                    val exported = exportLogsForPull()
                    _logUploadStatus.value = "dumped:${exported.size}"
                } catch (e: Exception) {
                    _logUploadStatus.value = "failed"
                }
            }
            return true
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "dumpAndUploadLogs failed: ${e.message}")
            return false
        }
    }

    private fun startMetaPolling() {
        if (!metaPollingInFlight.compareAndSet(false, true)) {
            RealTimeLogger.w(TAG, "startMetaPolling: already running — skipping duplicate start")
            return
        }

        metaPollingJob = viewModelScope.launch {
            RealTimeLogger.i(TAG, "Meta polling started (interval=${Timing.META_POLL_INTERVAL_MS}ms)")
            try {
                while (isActive) {
                    val iterStart = System.currentTimeMillis()
                    val nowTs = System.currentTimeMillis()
                    recentMetaRequests.addLast(nowTs)
                    while (recentMetaRequests.isNotEmpty() && nowTs - recentMetaRequests.first() > RATE_SHORT_WINDOW_MS) {
                        recentMetaRequests.removeFirst()
                    }

                    if (recentMetaRequests.size > RATE_MAX_REQUESTS_PER_WINDOW) {
                        metaBackoffMs = if (metaBackoffMs == 0L) RATE_BACKOFF_INITIAL_MS else (metaBackoffMs * 2).coerceAtMost(RATE_BACKOFF_MAX_MS)
                        RealTimeLogger.w(TAG, "Meta polling rate burst: ${recentMetaRequests.size} reqs in ${RATE_SHORT_WINDOW_MS}ms -> applying metaBackoffMs=${metaBackoffMs}ms")
                    } else {
                        if (metaBackoffMs > 0L) {
                            metaBackoffMs = (metaBackoffMs / 2).coerceAtLeast(0L)
                            if (metaBackoffMs == 0L) RealTimeLogger.i(TAG, "Meta polling backoff cleared")
                        }
                    }

                    try {
                        val meta = try { networkClient.fetchMeta(forceRefresh = true) } catch (e: Exception) { RealTimeLogger.w(TAG, "meta poll failed: ${e.message}"); null }

                        if (meta != null) {
                            val prev = lastKnownRound
                            if (prev == null || meta.round != prev) {
                                lastKnownRound = meta.round
                                persistLastKnownRound(meta.round)
                                _trainingDataStatus.value = "新しいラウンドのモデルが利用可能です (round=${meta.round})"
                                _trainingFinished.value = false
                                _errorMessage.value = null
                                if (prev == null) {
                                    RealTimeLogger.i(TAG, "Meta poll: initialized round=${meta.round}")
                                } else if (meta.round > prev) {
                                    RealTimeLogger.i(TAG, "Meta poll: detected new round ${meta.round} > $prev — enabling Start button")
                                } else {
                                    RealTimeLogger.i(TAG, "Meta poll: detected server round decreased from $prev to ${meta.round} — treating as new (server reset)")
                                }

                                try {
                                    viewModelScope.launch(Dispatchers.IO) {
                                        try {
                                            fetchAndApplyLatestGlobalModel("meta_poll")
                                        } catch (e: Exception) {
                                            RealTimeLogger.w(TAG, "Meta-detected fetch/apply coroutine failed: ${e.message}")
                                        }
                                    }
                                } catch (e: Exception) {
                                    RealTimeLogger.w(TAG, "Failed to launch immediate fetchSendToDevice coroutine: ${e.message}")
                                }
                            } else {
                                RealTimeLogger.w(TAG, "Meta poll: no change (round=${meta.round}), will wait before next attempt")
                            }
                        }
                    } finally {
                        val iterEnd = System.currentTimeMillis()
                        val elapsed = iterEnd - iterStart
                        val activeWaiting = awaitingModelAfterUpload || _isUploading.value
                        val chosenBaseMs = if (activeWaiting) Timing.META_POLL_INTERVAL_FAST_MS else Timing.META_POLL_INTERVAL_NORMAL_MS
                        val baseInterval = chosenBaseMs.coerceAtLeast(1000L)
                        val waitMs = (baseInterval - elapsed + metaBackoffMs).coerceAtLeast(0L)
                        RealTimeLogger.i(TAG, "Meta polling iteration finished elapsed=${elapsed}ms activeWaiting=${activeWaiting} metaBackoffMs=${metaBackoffMs}ms waiting=${waitMs}ms (base=${baseInterval}ms)")

                        try {
                            kotlinx.coroutines.delay(waitMs)
                        } catch (ce: CancellationException) {
                            RealTimeLogger.i(TAG, "Meta polling delay cancelled")
                            throw ce
                        }
                    }
                }
            } finally {
                metaPollingInFlight.set(false)
                RealTimeLogger.i(TAG, "Meta polling stopped")
                metaPollingJob = null
            }
        }
    }

    fun stopMetaPolling() {
        try {
            metaPollingJob?.cancel()
            metaPollingJob = null
            metaPollingInFlight.set(false)
            RealTimeLogger.i(TAG, "stopMetaPolling: requested — meta polling cancelled")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "stopMetaPolling failed: ${e.message}")
        }
    }

    /** 学習データを準備する。返す Job の完了後に isTrainingDataReady を見る（SP5 の自動開始が待つ）。 */
    fun fetchAndPrepareTrainingData(selectedApps: List<Int>? = null): Job {
         _trainingDataStatus.value = "学習データを準備中..."
         return viewModelScope.launch {
             try {
                withContext(Dispatchers.Default) {
                    val tpRttFeatures = 2
                    val appNumMax = if (appConfigs.isNullOrEmpty()) AppConfig.APP_CAT_COUNT else appConfigs!!.size
                    val totalSamples = 100

                    fun computeInitTpMbpsFromRttMs(rttMs: Float, windowBytes: Int = 65500 * 2): Float {
                        if (rttMs <= 0f) return 0f
                        val bits = windowBytes.toFloat() * 8f
                        val rttS = rttMs / 1000f
                        val tpBps = bits / rttS
                        return tpBps / 1_000_000f
                    }

                    trainingDataInfo = null  // 前回の記録を持ち越さない
                    // --- Prefer override files/training_data.csv, then bundled assets/training_data.csv ---
                    try {
                        val am = getApplication<Application>().assets
                        var assetLoaded: List<DataSample>? = null
                        var assetPresent = false
                        var trainingChoice: TrainingDataChoice? = null
                        try {
                            // rebuild-training-data-20261022: 端末の files/training_data.csv（上書き）をアセットより先に読む
                            trainingChoice = chooseTrainingCsv(
                                java.io.File(getApplication<Application>().filesDir, TRAINING_DATA_OVERRIDE_NAME),
                                { try { am.open("training_data.csv") } catch (_: Exception) { null } },
                                java.io.File(getApplication<Application>().cacheDir, "training_data_from_assets.csv"))
                        } catch (e: Exception) {
                            RealTimeLogger.w(TAG, "fetchAndPrepareTrainingData: training csv choice failed: ${e.message}")
                        }
                        trainingChoice?.let { choice ->
                            assetPresent = true
                            val lineCount = try { choice.file.useLines { it.count() } } catch (_: Exception) { -1 }
                            RealTimeLogger.i(TAG, "fetchAndPrepareTrainingData: training csv source=${choice.source} lines=${lineCount} sha256=${choice.sha256}")
                            assetLoaded = try { com.example.hfl_experiment.training.data.CsvLoader.loadFlexible(choice.file, 2, appNumMax) } catch (e: Exception) { null }
                            if (choice.source == "asset") { try { if (choice.file.exists()) choice.file.delete() } catch (_: Exception) {} }
                            if (!assetLoaded.isNullOrEmpty()) recordTrainingData(choice.source, choice.sha256, assetLoaded!!.size)
                        }

                        // If asset file exists but parser returned empty/null, raise explicit error so developer notices
                        if (assetPresent && assetLoaded.isNullOrEmpty()) {
                            _trainingDataStatus.value = "学習データ (assets) が存在しますがパースに失敗しました。アセットを確認してください。"
                            _errorMessage.value = "教師データ(${trainingChoice?.source ?: "assets"}) が存在するがパースに失敗しました。CSVの形式を確認してください。上書きファイルは scripts/set_training_data.sh --clear で消せます。"
                            RealTimeLogger.e(TAG, "fetchAndPrepareTrainingData: asset present but CsvLoader returned empty/null - aborting to avoid fallback synthetic data")
                            // mark not ready and return early (do not fallback to synthetic generation)
                            _isTrainingDataReady.value = false
                            return@withContext
                        }

                        if (!assetLoaded.isNullOrEmpty()) {
                            // copy to immutable val to avoid smart-cast issues
                            val asset = assetLoaded!!
                            // detect label range and possibly extend appNum
                            val maxLabel = asset.maxOfOrNull { it.label } ?: 0
                            val actualAppNum = (maxLabel + 1).coerceAtLeast(appNumMax)
                            val normalized: List<DataSample> = if (actualAppNum != appNumMax) {
                                asset.map { ds ->
                                    val feat = FloatArray(2 + actualAppNum)
                                    for (i in 0 until 2) feat[i] = ds.features.getOrElse(i) { 0f }
                                    val lbl = ds.label.coerceAtLeast(0)
                                    for (j in 0 until actualAppNum) feat[2 + j] = if (j == lbl) 1.0f else 0.0f
                                    DataSample(features = feat, label = ds.label)
                                }
                            } else asset

                            // Partition the entire normalized dataset among terminals (do NOT pre-filter by assigned app)
                            ensureAssignedAppInitialized(actualAppNum)
                            val tCount = readTerminalCount()
                            // E8: 番号は 設定 → 端末 ID の末尾の数字 → 0（以前は設定が無く全台 0 番だった）
                            val (myIdx, idxSource) = resolveTerminalIndex(readTerminalIndex(), terminalId, tCount)
                            val slice = partitionRows(normalized, myIdx, tCount)
                            recordDataPartition(myIdx, tCount, slice.size, idxSource)

                            // assign to fullDataset the slice for this terminal
                            fullDataset = slice
                             modelInputSize = 2 + actualAppNum
                             modelHiddenSize = 32
                             // 出力 = AP 数（先行研究準拠、ラベル = AP ID）。入力の one-hot 長（アプリ数）とは別
                             modelOutputSize = try { trainingOutputSize(maxLabel) } catch (e: IllegalArgumentException) {
                                 _errorMessage.value = e.message; _isTrainingDataReady.value = false
                                 RealTimeLogger.e(TAG, "fetchAndPrepareTrainingData: ${e.message}"); return@withContext
                             }
                             _trainingDataStatus.value = "学習データ取得完了 (assets slice ${slice.size} / total ${normalized.size} 件, appNum=$actualAppNum)"
                             _isTrainingDataReady.value = true
                             _errorMessage.value = null
                             RealTimeLogger.i(TAG, "fetchAndPrepareTrainingData: loaded ${normalized.size} samples from assets/training_data.csv; selected slice=${slice.size} for terminalIndex=$myIdx terminalCount=$tCount")
                             return@withContext
                         }
                    } catch (e: Exception) {
                        RealTimeLogger.w(TAG, "fetchAndPrepareTrainingData: asset load attempt failed: ${e.message}")
                    }

                     // Try to obtain training CSV from edge (best-effort). This path is used when the user
                     // presses the "Fetch Training Data" button manually. If the server returns a data path
                     // (paths.data_rel or links.data or data.path), download it and parse as CSV. On any
                     // failure, we fall back to the synthetic generation below.
                     try {
                        val reqId = java.util.UUID.randomUUID().toString()
                        val dto = try { networkClient.fetchSendToDeviceShort(timeoutMs = 5000L, reqId = reqId) } catch (e: Exception) { null }
                        if (dto != null) {
                            // Resolve candidate data path from various possible fields
                            val candidateRel = dto.paths?.data_rel ?: dto.links?.data ?: dto.data?.path
                            if (!candidateRel.isNullOrBlank()) {
                                try {
                                    val downloadBase = AppConfig.getDownloadBaseUrl(getApplication())
                                    // build URL: follow server convention similar to ModelUpdateUtil.buildDownloadUrl
                                    val url = if (candidateRel.startsWith("http://") || candidateRel.startsWith("https://")) {
                                        candidateRel
                                    } else {
                                        val baseTrim = downloadBase.trimEnd('/')
                                        if (baseTrim.contains("?")) "$baseTrim&rel_path=$candidateRel" else "$baseTrim?rel_path=$candidateRel"
                                    }

                                    RealTimeLogger.i("TrainingViewModel", "Attempting to download training CSV from $url")
                                    val bytes = try { networkClient.downloadUrl(url) } catch (e: Exception) { null }
                                    if (bytes != null && bytes.isNotEmpty()) {
                                        val tmp = java.io.File(getApplication<Application>().cacheDir, "training_data_${System.currentTimeMillis()}.csv")
                                        tmp.writeBytes(bytes)
                                        try {
                                            // load CSV using existing CsvLoader (featureCount = tpRttFeatures + appNumMax)
                                            val loaded = com.example.hfl_experiment.training.data.CsvLoader.loadFlexible(tmp, tpRttFeatures, appNumMax)
                                            if (!loaded.isNullOrEmpty()) {
                                                // detect actual label max to ensure one-hot sizing matches dataset
                                                val maxLabel = loaded.maxOfOrNull { it.label } ?: 0
                                                val actualAppNum = (maxLabel + 1).coerceAtLeast(appNumMax)
                                                val normalized = if (actualAppNum != appNumMax) {
                                                    // rebuild feature vectors with extended one-hot
                                                    loaded.map { ds ->
                                                        val feat = FloatArray(tpRttFeatures + actualAppNum)
                                                        // copy tp/rtt
                                                        for (i in 0 until tpRttFeatures) feat[i] = ds.features[i]
                                                        // set one-hot
                                                        val lbl = ds.label.coerceAtLeast(0)
                                                        for (j in 0 until actualAppNum) feat[tpRttFeatures + j] = if (j == lbl) 1.0f else 0.0f
                                                        DataSample(features = feat, label = ds.label)
                                                    }
                                                } else loaded

                                                // Partition the entire normalized dataset among terminals (do NOT pre-filter by assigned app)
                                                ensureAssignedAppInitialized(actualAppNum)
                                                val tCount = readTerminalCount()
                                                // E8: 番号は 設定 → 端末 ID の末尾の数字 → 0（以前は設定が無く全台 0 番だった）
                                                val (myIdx, idxSource) = resolveTerminalIndex(readTerminalIndex(), terminalId, tCount)
                                                val slice = partitionRows(normalized, myIdx, tCount)
                                                recordDataPartition(myIdx, tCount, slice.size, idxSource)
                                                recordTrainingData("server", sha256Hex(tmp), loaded.size)

                                                fullDataset = slice
                                                 modelInputSize = tpRttFeatures + actualAppNum
                                                 modelHiddenSize = 32
                                                 // 出力 = AP 数（先行研究準拠、ラベル = AP ID）
                                                 modelOutputSize = try { trainingOutputSize(maxLabel) } catch (e: IllegalArgumentException) {
                                                     _errorMessage.value = e.message; _isTrainingDataReady.value = false
                                                     RealTimeLogger.e(TAG, "fetchAndPrepareTrainingData: ${e.message}"); return@withContext
                                                 }
                                                 _trainingDataStatus.value = "学習データ取得完了 (server slice ${slice.size} / total=${normalized.size} 件, appNum=$actualAppNum)"
                                                 _isTrainingDataReady.value = true
                                                 _errorMessage.value = null
                                                 RealTimeLogger.i(TAG, "fetchAndPrepareTrainingData: selected slice size=${slice.size} of total=${normalized.size} for terminalIndex=${myIdx} / terminalCount=${tCount} (appNum=$actualAppNum)")
                                                 try { if (tmp.exists()) tmp.delete() } catch (_: Exception) {}
                                                 return@withContext
                                             } else {
                                                 RealTimeLogger.w(TAG, "fetchAndPrepareTrainingData: assets training_data.csv parsed but empty")
                                             }
                                        } catch (e: Exception) {
                                            RealTimeLogger.w(TAG, "fetchAndPrepareTrainingData: failed to load training_data.csv from assets: ${e.message}")
                                        } finally {
                                            try { if (tmp.exists()) tmp.delete() } catch (_: Exception) {}
                                        }
                                    } else {
                                        RealTimeLogger.w(TAG, "fetchAndPrepareTrainingData: download returned empty or null bytes for $url")
                                    }
                                } catch (e: Exception) {
                                    RealTimeLogger.w(TAG, "fetchAndPrepareTrainingData: failed to download candidateRel=${candidateRel}: ${e.message}")
                                }
                            } else {
                                RealTimeLogger.i(TAG, "fetchAndPrepareTrainingData: send_to_device returned no data path;")
                                _errorMessage.value = "サーバーから学習データが提供されませんでした"
                                _isTrainingDataReady.value = false
                                return@withContext
                            }
                        } else {
                            RealTimeLogger.i(TAG, "fetchAndPrepareTrainingData: fetchSendToDeviceShort returned null (no DTO)")
                            _errorMessage.value = "サーバーから学習データが提供されませんでした"
                            _isTrainingDataReady.value = false
                            return@withContext
                        }
                    } catch (e: Exception) {
                        RealTimeLogger.w(TAG, "fetchAndPrepareTrainingData: sendToDevice attempt failed: ${e.message}")
                        _errorMessage.value = "サーバー通信エラー"
                        _isTrainingDataReady.value = false
                        return@withContext
                    }
                }
            } catch (e: Exception) {
                _trainingDataStatus.value = "データ準備エラー"
                _errorMessage.value = "データの準備に失敗しました: ${e.message}"
                RealTimeLogger.e(TAG, "Data preparation failed", e)
            }
        }
    }

    private var tpMean: Float = 0f
    private var tpStd: Float = 1f
    private var rttMean: Float = 0f
    private var rttStd: Float = 1f

    @Suppress("unused")
    fun getNormalizationStats(): Map<String, Float> = mapOf(
        "tp_mean" to tpMean,
        "tp_std" to tpStd,
        "rtt_mean" to rttMean,
        "rtt_std" to rttStd
    )

    @Suppress("unused")
    fun startTraining(inputSize: Int, hiddenSize: Int, outputSize: Int, epochs: Int) {
        if (!isTrainingDataReady.value || fullDataset.isEmpty()) {
            _errorMessage.value = "学習データが空であるか、準備できていません。"
            RealTimeLogger.w(TAG, "startTraining: fullDataset is empty or not ready.")
            return
        }
        try { stopMetaPolling() } catch (_: Exception) {}
        try { TrainingSimpleLogger.init(getApplication()) } catch (_: Exception) {}
        try { ServerStyleLogger.init(getApplication()) } catch (_: Exception) {}

        modelInputSize = inputSize
        modelHiddenSize = hiddenSize
        modelOutputSize = outputSize

        _trainingProgress.value = 0f
        _trainingFinished.value = false
        _errorMessage.value = null

        val trainingScope = CoroutineScope(Dispatchers.Default)
        trainingScope.launch {
            withContext(Dispatchers.Main) { _isTrainingRunning.value = true }

            try {
                val dlSha = downloadedModelSha()
                localTrainer = LocalTrainer(
                    fullDataset = fullDataset,
                    userIndices = fullDataset.indices.toList(),
                    inputSize = inputSize,
                    hiddenSize = hiddenSize,
                    outputSize = outputSize,
                    dropoutP = 0.1f,
                    learningRate = 0.0001f,
                    weightDecay = 0.01f,
                    localEpochs = epochs,
                    localBatchSize = 16,
                    clipMaxNorm = 1.0f,
                    warmupSteps = 10,
                    totalSteps = (if (fullDataset.isNotEmpty()) (fullDataset.size / 16) * epochs else epochs),
                    minLrRatio = 0.1f,
                    logger = { message -> RealTimeLogger.d("LocalTrainer", message) }
                    , appFilesDir = getApplication<Application>().filesDir
                    , initialParams = chooseTrainingInit(dlSha, lastUsedDownloadedSha, lastTrainedParams)
                )
                recordTrainingInit(localTrainer, dlSha)
                try {
                    val splits = localTrainer?.getSplitSizes()
                    if (splits != null) {
                        RealTimeLogger.i(TAG, "startTraining: dataset splits -> train=${splits.first} val=${splits.second} test=${splits.third}")
                        try { android.util.Log.d("HFL", "Test dataset size: ${splits.third}") } catch (_: Exception) {}
                    }
                } catch (_: Exception) {}

                val epochCb: (Int, Float, Float) -> Unit = { epoch, valLoss, valAcc ->
                    // Update StateFlow directly (MutableStateFlow is thread-safe) to avoid spawning many coroutines
                    _trainingProgress.value = epoch.toFloat() / epochs.toFloat()
                    try { _currentEpoch.value = epoch } catch (_: Exception) {}
                    try { _currentCycle.value = 1 } catch (_: Exception) {}
                    try { TrainingSimpleLogger.appendEpoch(epoch, valLoss.toDouble(), valAcc.toDouble(), null) } catch (_: Exception) {}
                }
                val progressCb: (Float) -> Unit = { pf ->
                    // update StateFlow directly from background thread to reduce main-thread scheduling overhead
                    _trainingProgress.value = pf
                    try { TrainingSimpleLogger.appendProgress(pf) } catch (_: Exception) {}
                }

                localTrainer?.updateWeightsAndGetLoss({ epoch, valLoss, valAcc ->
                    try { epochCb(epoch, valLoss, valAcc) } catch (_: Exception) {}
                }, { progressFraction -> progressCb(progressFraction) })
                lastTrainedParams = localTrainer?.getModelParameters()  // 次サイクルの継続用（衝突②）

                withContext(Dispatchers.Main) {
                    _trainingFinished.value = true
                    _trainingProgress.value = 1.0f
                }

                try {
                    try { ServerStyleLogger.append("INFO", "TrainingViewModel", "startTraining: training started for epochs=${epochs}") } catch (_: Exception) {}
                     RealTimeLogger.i(TAG, "startTraining: about to call uploadUpdateSuspend()")
                    withContext(Dispatchers.IO) {
                        // 手動アップロード(uploadSlot)と排他
                        val ok = uploadSlot.mutex.withLock { uploadUpdateSuspend() }
                        RealTimeLogger.i(TAG, "startTraining: uploadUpdateSuspend returned ok=$ok")
                        if (!ok) {
                            RealTimeLogger.w(TAG, "uploadUpdateSuspend returned false from startTraining")
                            withContext(Dispatchers.Main) { _errorMessage.value = "アップロードに失敗しました" }
                        } else {
                            RealTimeLogger.i(TAG, "startTraining: Immediate upload after training succeeded")
                        }
                    }
                } catch (e: CancellationException) {
                    RealTimeLogger.i(TAG, "Upload cancelled")
                    withContext(Dispatchers.Main) { _errorMessage.value = "アップロードがキャンセルされました" }
                } catch (e: Exception) {
                    RealTimeLogger.e(TAG, "Upload failed", e)
                    withContext(Dispatchers.Main) { _errorMessage.value = "アップロード中にエラーが発生しました: ${e.message}" }
                }

            } catch (e: Exception) {
                RealTimeLogger.e(TAG, "Training failed", e)
                withContext(Dispatchers.Main) {
                    _errorMessage.value = "学習処理でエラーが発生しました: ${e.message}"
                    _trainingFinished.value = false
                }
            } finally {
                withContext(Dispatchers.Main) { _isTrainingRunning.value = false }
            }
        }
    }

    @Suppress("UNUSED_PARAMETER")
    private suspend fun performLocalTrainingSuspend(
        inputSize: Int,
        hiddenSize: Int,
        outputSize: Int,
        epochs: Int,
        onProgress: (Float) -> Unit = {},
        onEpoch: (Int) -> Unit = {},
        initialParams: ModelParameters? = null
    ): Boolean {
        // publish epochs-per-cycle for UI
        _epochsPerCycle.value = epochs

        if (!isTrainingDataReady.value) return false
        try { TrainingSimpleLogger.init(getApplication()) } catch (_: Exception) {}

        modelInputSize = inputSize
        modelHiddenSize = hiddenSize
        modelOutputSize = outputSize

        _trainingProgress.value = 0f
        _trainingFinished.value = false
        _errorMessage.value = null

        val epochDurations = mutableListOf<Long>()
        val totalStart = System.currentTimeMillis()

        return try {
            withContext(Dispatchers.Default) {
                val dlSha = downloadedModelSha()
                localTrainer = LocalTrainer(
                    fullDataset = fullDataset,
                    userIndices = fullDataset.indices.toList(),
                    inputSize = inputSize,
                    hiddenSize = hiddenSize,
                    outputSize = outputSize,
                    dropoutP = 0.1f,
                    learningRate = 0.0001f,
                    weightDecay = 0.01f,
                    localEpochs = epochs,
                    localBatchSize = 16,
                    clipMaxNorm = 1.0f,
                    warmupSteps = 10,
                    totalSteps = (fullDataset.size / 16) * epochs,
                    minLrRatio = 0.1f,
                    logger = { message -> RealTimeLogger.d("LocalTrainer", message) }
                    , appFilesDir = getApplication<Application>().filesDir
                    , initialParams = chooseTrainingInit(dlSha, lastUsedDownloadedSha, initialParams ?: lastTrainedParams)
                )
                recordTrainingInit(localTrainer, dlSha)
                try {
                    val splits = localTrainer?.getSplitSizes()
                    if (splits != null) {
                        RealTimeLogger.i(TAG, "performLocalTrainingSuspend: dataset splits -> train=${splits.first} val=${splits.second} test=${splits.third}")
                        try { android.util.Log.d("HFL", "Test dataset size: ${splits.third}") } catch (_: Exception) {}
                    }
                } catch (_: Exception) {}

                // initialize metric collection
                epochMetrics.clear()

                // sample CPU and memory before training
                try {
                    lastTrainingCpuMsUsed = null
                    lastHeapBeforeBytes = Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory()
                    lastNativeHeapBeforeBytes = android.os.Debug.getNativeHeapAllocatedSize()
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "pre-training resource sample failed: ${e.message}")
                }

                val cpuStart = try { android.os.Process.getElapsedCpuTime() } catch (e: Exception) { 0L }
                val lastEpochTs = java.util.concurrent.atomic.AtomicLong(System.currentTimeMillis())

                // capture battery before training (best-effort)
                try { batteryLevelBeforeTraining = readBatteryLevelPercent() } catch (_: Exception) {}

                // Mark compute start for network_idle_wait_ms calculation
                com.example.hfl_experiment.telemetry.DeviceMetricsCollector.markComputeStart()

                localTrainer?.updateWeightsAndGetLoss({ epoch, valLoss, valAcc ->
                    val now = System.currentTimeMillis()
                    val dur = now - lastEpochTs.get()
                    lastEpochTs.set(now)
                    epochDurations.add(dur)
                    try { onProgress(epoch.toFloat() / epochs.toFloat()) } catch (_: Exception) {}
                    try { onEpoch(epoch) } catch (_: Exception) {}
                    try {
                        // valLoss and valAcc are non-null Floats coming from the trainer; avoid unnecessary safe-call
                        epochMetrics.add(EpochMetric(epoch = epoch, durationMs = dur, valLoss = valLoss.toDouble(), valAcc = valAcc.toDouble()))
                        // also log each epoch metric explicitly
                        RealTimeLogger.d(TAG, "EpochMetric: epoch=${epoch} dur=${dur}ms valLoss=${valLoss} valAcc=${valAcc}")
                        try { TrainingSimpleLogger.appendEpoch(epoch, valLoss.toDouble(), valAcc.toDouble(), null) } catch (_: Exception) {}
                    } catch (_: Exception) {}
                    // Track epoch completion + memory pressure check
                    try {
                        com.example.hfl_experiment.telemetry.DeviceMetricsCollector.recordEpochCompleted()
                        com.example.hfl_experiment.telemetry.DeviceMetricsCollector.checkMemoryPressure(getApplication())
                    } catch (_: Exception) {}
                }, { progressFraction ->
                    try { onProgress(progressFraction) } catch (_: Exception) {}
                })
                lastTrainedParams = localTrainer?.getModelParameters()  // 次サイクルの継続用（衝突②）

                // capture battery after training
                try { batteryLevelAfterTraining = readBatteryLevelPercent() } catch (_: Exception) {}

                val cpuEnd = try { android.os.Process.getElapsedCpuTime() } catch (e: Exception) { 0L }
                try {
                    lastTrainingCpuMsUsed = (cpuEnd - cpuStart)
                    lastHeapAfterBytes = Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory()
                    lastNativeHeapAfterBytes = android.os.Debug.getNativeHeapAllocatedSize()
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "post-training resource sample failed: ${e.message}")
                }

                lastEpochDurationsMs = epochDurations.toList()
                lastLocalTrainingTotalMs = System.currentTimeMillis() - totalStart

                true
            }
        } catch (e: CancellationException) {
            RealTimeLogger.i(TAG, "performLocalTrainingSuspend cancelled")
            _errorMessage.value = "学習がキャンセルされました"
            throw e
        } catch (e: Exception) {
            RealTimeLogger.e(TAG, "performLocalTrainingSuspend failed", e)
            _errorMessage.value = "学習中に例外が発生しました: ${e.message}"
            false
        }
    }

    // Flatten model parameters into a single float array for hashing/inspection
    private fun buildFlatWeights(p: ModelParameters): FloatArray {
        val list = buildList<Float> {
            addAll(p.weights1.flatten()); addAll(p.biases1)
            addAll(p.norm1Gamma); addAll(p.norm1Beta)
            addAll(p.weights2.flatten()); addAll(p.biases2)
            addAll(p.norm2Gamma); addAll(p.norm2Beta)
            addAll(p.weights3.flatten()); addAll(p.biases3)
        }
        val sane = list.map { if (it.isFinite()) it else 0f }
        return sane.toFloatArray()
    }

    private suspend fun uploadUpdateSuspend(): Boolean {
        val uploadTraceCookie = com.example.hfl_experiment.telemetry.HflTracer.beginAsync("HFL_upload")
        val trainer = localTrainer
        if (trainer == null) {
            _errorMessage.value = "エラー: 学習が未実行です。"
            RealTimeLogger.e(TAG, "uploadUpdateSuspend called but localTrainer is null.")
            com.example.hfl_experiment.telemetry.HflTracer.endAsync("HFL_upload", uploadTraceCookie)
            return false
        }
        if (modelInputSize == 0 || modelHiddenSize == 0 || modelOutputSize == 0) {
            _errorMessage.value = "エラー: モデル寸法が未設定です（学習前提）。"
            return false
        }

        _isUploading.value = true
        _uploadProgress.value = 0f
        _uploadAttempt.value = 0

        try {
            return withContext(Dispatchers.IO) {
                var attempt = 0
                var delayMs = BASE_UPLOAD_RETRY_DELAY_MS
                var lastException: Exception? = null

                while (attempt < MAX_UPLOAD_RETRIES) {
                    ensureActive()
                    attempt++
                    _uploadAttempt.value = attempt
                    var uploadSucceeded = false
                    try {
                        // Emit a clear debug marker for upload flow and add a short jitter delay to avoid burst throttling
                        val jitterMs = 1000L + kotlin.random.Random.nextLong(0L, 2000L)
                        lastUploadPreDelayMs = jitterMs
                        try { RealTimeLogger.d("HFL_DEBUG", "UPLOAD_FLOW: ATTEMPT_UPLOAD attempt=${attempt} nSamples=${fullDataset.size} terminalId=${terminalId} preDelayMs=${jitterMs}") } catch (_: Throwable) {}
                        try { kotlinx.coroutines.delay(jitterMs) } catch (ce: CancellationException) { throw ce }

                        RealTimeLogger.i(TAG, "uploadUpdateSuspend: attempt=$attempt about to start with timeout=${UPLOAD_TIMEOUT_MS}ms; nSamples=${fullDataset.size}, terminalId=${terminalId}")
                        // declare baseline round here so it is visible after the withTimeout block
                        var metaRoundAtUpload: Int = lastKnownRound ?: 0
                        withUploadAttemptTimeout(UPLOAD_TIMEOUT_MS) {
                            try { RealTimeLogger.d("HFL_DEBUG", "UPLOAD_FLOW: starting networkClient.uploadModelParameters call attempt=${attempt}") } catch (_: Throwable) {}
                            // Try to obtain cached meta from NetworkClient (no force refresh) so we don't trigger extra HTTP requests
                            val meta = try {
                                 networkClient.fetchMeta(forceRefresh = false)
                             } catch (e: Exception) {
                                 RealTimeLogger.w(TAG, "uploadUpdateSuspend: fetch cached meta failed: ${e.message} - falling back to lastKnownRound")
                                 MetaResp(
                                     round = lastKnownRound ?: 0,
                                     model_id = "",
                                     base_hash = "",
                                     preferred_dtype = null
                                 )
                             }
                            // baseline round observed at upload start - used later to wait for server to advance
                            metaRoundAtUpload = meta.round

                            val serializeStart = System.currentTimeMillis()
                            val params: ModelParameters = withContext(Dispatchers.Default) { trainer.getModelParameters() }
                            val serializeEnd = System.currentTimeMillis()
                            try { com.example.hfl_experiment.telemetry.DeviceMetricsCollector.recordThreadWait(serializeEnd - serializeStart) } catch (_: Throwable) {}
                            try { lastUploadedModelParameters = params } catch (_: Throwable) { }

                            val nSamples = fullDataset.size
                            val appIdx = _assignedAppIndex.value ?: 0
                            val clientMeta = JSONObject().apply {
                                put("terminal_id", terminalId)
                                put("total_local_ms", lastLocalTrainingTotalMs)
                                put("epoch_durations_ms", JSONArray(lastEpochDurationsMs))
                                put("timestamp_ms", System.currentTimeMillis())
                                put("virtual_router_id", currentRouterId)
                                // Research data fields — required for per-experiment analysis
                                put("session_id", currentSessionId.ifEmpty { "no_session" })
                                put("run_id", com.example.hfl_experiment.experiment.ExperimentContext.runId.ifEmpty { "none" })
                                put("experiment_group", com.example.hfl_experiment.experiment.ExperimentContext.experimentGroup)
                                put("app_index", appIdx)
                                put("app_type", appConfigs?.getOrNull(appIdx)?.appType ?: "unknown")
                                put("satisfaction_before", sessionSatisfactionBefore ?: JSONObject.NULL)
                                put("satisfaction_after", sessionSatisfactionAfter ?: JSONObject.NULL)
                                put("cycle_number", _currentCycle.value)
                                put("baseline_mode", AppConfig.BASELINE_MODE)
                                put("baseline_mode_source", AppConfig.baselineModeSource.value)
                                put("hfl_mode", AppConfig.hflMode)
                                put("hfl_mode_source", AppConfig.hflModeSource.value)
                                put("init_source", lastInitSource)
                                put("init_model_sha256", lastInitModelSha ?: JSONObject.NULL)
                                putDataPartition(this, dataPartition)  // E8
                                putTrainingData(this, trainingDataInfo)  // rebuild-training-data
                                putConnectedAp(this, readConnectedAp())  // record-connected-ap
                                put("tp_unit", com.example.hfl_experiment.util.networking.NetworkBandwidthProbe.TP_UNIT)  // E7
                                put("network_type_at_upload", getNetworkTransport())
                                put("battery_at_start", batteryLevelBeforeTraining ?: JSONObject.NULL)
                                put("battery_at_end", batteryLevelAfterTraining ?: JSONObject.NULL)
                                put("model_version", com.example.hfl_experiment.experiment.ExperimentContext.modelVersion)
                                put("tp_measured_mbps", lastBandwidthMbps ?: JSONObject.NULL)
                                put("rtt_measured_ms", lastMeasuredRttMs ?: JSONObject.NULL)
                                // Accuracy / Loss: アップロードする重み（最終エポック後）の検証値（SP-M, 旧: エポック平均）
                                val _ep = summarizeEpochs(epochMetrics.map { it.valAcc }, epochMetrics.map { it.valLoss })
                                put("accuracy", _ep.lastAcc ?: JSONObject.NULL)
                                put("loss", _ep.lastLoss ?: JSONObject.NULL)
                            }.let { cm -> currentCycleRow?.let { embedMetricInClientMeta(cm, it) } ?: cm }.toString()

                            RealTimeLogger.i(TAG, "uploadUpdateSuspend: calling networkClient.uploadModelParameters (attempt=$attempt) skipFetchMeta=true")

                            // record network transport at upload time
                            lastNetworkTransport = getNetworkTransport()

                            // compute SHA256 of weights (best-effort)
                            try {
                                val flat = this@TrainingViewModel.buildFlatWeights(params)
                                lastUploadedWeightsSha256 = sha256HexFromFloatArray(flat)
                                RealTimeLogger.d(TAG, "Computed weights SHA256=${lastUploadedWeightsSha256}")
                            } catch (e: Exception) {
                                RealTimeLogger.w(TAG, "Failed to compute weights SHA256: ${e.message}")
                            }

                            val uploadStart = System.currentTimeMillis()
                            val uploadResp = try {
                                networkClient.uploadModelParameters(
                                    terminalId = terminalId,
                                    meta = meta,
                                    nSamples = nSamples,
                                    params = params,
                                    payloadKind = "full",
                                    inputSize = modelInputSize,
                                    hiddenSize = modelHiddenSize,
                                    outputSize = modelOutputSize,
                                    clientMetaJson = clientMeta,
                                    virtualRouterId = currentRouterId,
                                    progressCallback = { sent, total ->
                                        try {
                                            val prog = if (total > 0L) (sent.toFloat() / total.toFloat()).coerceIn(0f, 1f) else 0f
                                            _uploadProgress.value = prog
                                        } catch (_: Throwable) { }
                                    },
                                    skipFetchMeta = true
                                )
                            } catch (e: Exception) {
                                try { RealTimeLogger.e("HFL_DEBUG", "UPLOAD_FLOW: uploadModelParameters threw attempt=${attempt} err=${e::class.java.simpleName} ${e.message}") } catch (_: Throwable) {}
                                RealTimeLogger.e(TAG, "uploadUpdateSuspend: networkClient.uploadModelParameters threw on attempt=$attempt: ${e::class.java.simpleName} ${e.message}")
                                RealTimeLogger.e(TAG, "uploadUpdateSuspend: full stack: ${e.stackTraceToString()}")
                                throw e
                            } finally {
                                val uploadEnd = System.currentTimeMillis()
                                lastUploadPostMs = uploadEnd - uploadStart
                                RealTimeLogger.i(TAG, "uploadUpdateSuspend: upload POST duration=${lastUploadPostMs}ms")
                            }

                            val respPreview = try { uploadResp.toString().take(400) } catch (_: Exception) { "" }
                            RealTimeLogger.i(TAG, "uploadUpdateSuspend: uploadModelParameters completed attempt=$attempt responsePreview=${respPreview}")
                            val respPreviewShort = respPreview.take(200)
                            RealTimeLogger.d("HFL_DEBUG", "UPLOAD_FLOW: uploadModelParameters completed attempt=${attempt} responsePreview=${respPreviewShort}")

                            RealTimeLogger.i(TAG, "モデル更新のアップロードに成功しました。（attempt ${attempt}）")
                            _uploadProgress.value = 1.0f
                            uploadSucceeded = true
                        }

                        if (uploadSucceeded) {
                            // record the round baseline for subsequent wait
                            try { uploadedRoundAfterUpload = metaRoundAtUpload } catch (_: Throwable) {}
                            try { ServerStyleLogger.append("INFO", "TrainingViewModel", "upload succeeded attempt=${attempt} uploaded_round_baseline=${metaRoundAtUpload}") } catch (_: Exception) {}
                            // Mark network complete for idle_wait calculation
                            try { com.example.hfl_experiment.telemetry.DeviceMetricsCollector.markNetworkComplete() } catch (_: Exception) {}
                            // Record upload timing for payload vs total ratio
                            try {
                                val totalMs = lastUploadPostMs ?: 0L
                                // payload transfer ≈ total - jitter delay (pre-delay is HTTP overhead/handshake)
                                val payloadMs = totalMs - (lastUploadPreDelayMs ?: 0L).coerceAtMost(totalMs)
                                com.example.hfl_experiment.telemetry.DeviceMetricsCollector.recordUploadTiming(payloadMs.coerceAtLeast(0L), totalMs)
                            } catch (_: Exception) {}
                            lastException = null
                            break
                        }
                    } catch (ce: CancellationException) {
                        RealTimeLogger.i(TAG, "uploadUpdateSuspend cancelled")
                        throw ce
                    } catch (e: Exception) {
                        try { RealTimeLogger.e("HFL_DEBUG", "UPLOAD_FLOW: upload attempt failed attempt=${attempt} err=${e::class.java.simpleName} ${e.message}") } catch (_: Throwable) {}
                        lastException = e
                        val retryable = when (e) {
                            is java.net.SocketTimeoutException -> true
                            is java.io.IOException -> true
                            is retrofit2.HttpException -> {
                                val code = e.code()
                                code == 429 || code >= 500
                            }
                            else -> false
                        }

                        val userMsg = when (e) {
                            is java.net.SocketTimeoutException -> "接続タイムアウトが発生しました"
                            is java.io.IOException -> "ネットワークエラーが発生しました"
                            is retrofit2.HttpException -> "サーバーエラー: HTTP ${e.code()}"
                            else -> "不明なエラー: ${e.message}"
                        }

                        RealTimeLogger.w(TAG, "upload attempt $attempt failed: ${e::class.java.simpleName}: ${e.message} (retryable=$retryable)\nstack=${e.stackTraceToString()}")

                        if (!retryable) {
                            _errorMessage.value = "アップロードに失敗しました: ${userMsg}"
                            break
                        }

                        _errorMessage.value = "アップロード失敗 (${attempt}/${MAX_UPLOAD_RETRIES})。${userMsg}。${(delayMs/1000)}秒後に再試行します。"
                        try {
                            RealTimeLogger.i(TAG, "uploadUpdateSuspend: backing off for ${delayMs}ms before next retry")
                            kotlinx.coroutines.delay(delayMs)
                        } catch (ie: CancellationException) {
                            RealTimeLogger.i(TAG, "upload backoff delay cancelled")
                            throw ie
                        }
                        delayMs = (delayMs * 2).coerceAtMost(30_000L)
                    }
                }

                if (lastException != null) {
                    RealTimeLogger.e(TAG, "uploadUpdateSuspend: all attempts failed", lastException)
                    _errorMessage.value = "アップロードに失敗しました: ${lastException.message}"
                    return@withContext false
                }

                try {
                    // Persist the just-uploaded model parameters to a file so the trained weights survive process restarts
                    try {
                        lastUploadedModelParameters?.let { p ->
                            val ts = System.currentTimeMillis()
                            val fname = "weights_uploaded_${ts}.json"
                            try {
                                writeModelParametersToFile(p, fname)
                                RealTimeLogger.i(TAG, "Saved uploaded model parameters to file=$fname")
                            } catch (e: Exception) {
                                RealTimeLogger.w(TAG, "Failed to save model parameters to file: ${e.message}")
                            }
                        }
                    } catch (e: Exception) {
                        RealTimeLogger.w(TAG, "persisting uploaded params failed: ${e.message}")
                    }

                    awaitingModelAfterUpload = true
                    // uploadedRoundAfterUpload already set to metaRoundAtUpload above on success
                    try { startMetaPolling() } catch (e: Exception) { RealTimeLogger.w(TAG, "startMetaPolling after upload failed: ${e.message}") }
                     // Enqueue a WorkManager job to ensure logs are uploaded durably in background
                     try {
                         com.example.hfl_experiment.work.UploadLogsWorker.enqueueOnce(getApplication())
                         RealTimeLogger.i(TAG, "Enqueued UploadLogsWorker for durable post-upload submission")
                     } catch (e: Exception) {
                         RealTimeLogger.w(TAG, "Failed to enqueue UploadLogsWorker: ${e.message}")
                     }
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "post-upload handling failed: ${e.message}")
                }

                true
            }
        } catch (e: CancellationException) {
            _errorMessage.value = "アップロードがキャンセルされました"
            RealTimeLogger.i(TAG, "uploadUpdateSuspend cancelled")
            return false
        } catch (e: Exception) {
            _errorMessage.value = "アップロードエラー: ${e.message}"
            RealTimeLogger.e(TAG, "Upload failed", e)
            return false
        } finally {
            _isUploading.value = false
            _uploadProgress.value = 0f
            _uploadAttempt.value =  0
            com.example.hfl_experiment.telemetry.HflTracer.endAsync("HFL_upload", uploadTraceCookie)
        }
    }

    @Suppress("UNUSED_PARAMETER")
    fun uploadUpdate() {
        RealTimeLogger.i(TAG, "uploadUpdate: launching upload job")
        // 自動アップロード（学習ジョブ内）が送信中なら重ねない: 待ってから同じ重みを再送するだけになるため
        if (uploadSlot.mutex.isLocked && !uploadSlot.isActive) {
            RealTimeLogger.i(TAG, "uploadUpdate: auto upload in progress; manual upload ignored")
            _errorMessage.value = "自動アップロード中のため手動送信は行いません"
            return
        }
        // 既存の手動アップロードは止めてから開始する（Retry Now / 再押下の二重アップロード防止）。
        uploadSlot.launch(viewModelScope) {
            try {
                val ok = uploadUpdateSuspend()
                RealTimeLogger.i(TAG, "uploadUpdate: uploadUpdateSuspend returned ok=$ok")
                if (!ok) {
                    RealTimeLogger.w(TAG, "uploadUpdate: upload failed")
                }
            } catch (e: Exception) {
                RealTimeLogger.e(TAG, "uploadUpdate: exception while uploading", e)
            }
        }
    }

    @Suppress("UNUSED_PARAMETER")
    fun cancelUpload() {
        try {
            // 参照は保持したまま止める: 直後の再アップロードが旧ジョブの終了を待てるように
            uploadSlot.cancel()
            _isUploading.value = false
            _errorMessage.value = "アップロードがキャンセルされました"
            RealTimeLogger.i(TAG, "cancelUpload: cancelled upload job")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "cancelUpload failed: ${e.message}")
        }
    }

    fun setPollParams(maxAttempts: Int, delayMs: Long) {
        try {
            _pollMaxAttempts.value = maxAttempts.coerceAtLeast(1)
            _pollDelayMs.value = delayMs.coerceAtLeast(500L)
            RealTimeLogger.i(TAG, "setPollParams: maxAttempts=${_pollMaxAttempts.value} delayMs=${_pollDelayMs.value}")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "setPollParams failed: ${e.message}")
        }
    }

    fun cancelRunFiveCycles() {
        try {
            RealTimeLogger.i(TAG, "cancelRunFiveCycles: cancelling current run/upload/polling jobs if present")
            try {
                _trainingDataStatus.value = "Stop: 中断要求を送りました（エポック／バッチ境界で停止します）"
            } catch (_: Exception) {}
            try { currentRunJob?.cancel() } catch (e: Exception) { RealTimeLogger.w(TAG, "cancel currentRunJob failed: ${e.message}") }
            currentRunJob = null
            try { uploadSlot.cancel() } catch (e: Exception) { RealTimeLogger.w(TAG, "cancel upload failed: ${e.message}") }
            try { metaPollingJob?.cancel() } catch (e: Exception) { RealTimeLogger.w(TAG, "cancel metaPollingJob failed: ${e.message}") }
            metaPollingJob = null
            awaitingModelAfterUpload = false
            RealTimeLogger.i(TAG, "cancelRunFiveCycles: cancellation requested")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "cancelRunFiveCycles failed: ${e.message}")
        }
    }

    private fun restoreRoundState() {
        try {
            if (lastKnownRound == null) {
                val sp = getApplication<Application>().getSharedPreferences("training_prefs", Context.MODE_PRIVATE)
                if (sp.contains("last_known_round")) {
                    val r = sp.getInt("last_known_round", -1)
                    if (r >= 0) {
                        lastKnownRound = r
                        RealTimeLogger.d(TAG, "restoreRoundState: restored lastKnownRound from prefs=$lastKnownRound")
                    } else {
                        RealTimeLogger.d(TAG, "restoreRoundState: no valid stored last_known_round")
                    }
                }
            } else {
                RealTimeLogger.d(TAG, "restoreRoundState: skipping restore because lastKnownRound already set by network fetch: $lastKnownRound")
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "restoreRoundState failed: ${e.message}")
        }
    }

    @Suppress("unused")
    fun clearSavedRound() {
        try {
            val sp = getApplication<Application>().getSharedPreferences("training_prefs", Context.MODE_PRIVATE)
            sp.edit { remove("last_known_round") }
            RealTimeLogger.i(TAG, "clearSavedRound: cleared persisted last_known_round")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "clearSavedRound failed: ${e.message}")
        }
    }

    fun resetTrainingFinishedFlag() {
        try {
            _trainingFinished.value = false
            _errorMessage.value = null
            RealTimeLogger.i(TAG, "resetTrainingFinishedFlag: cleared training finished flag and errors")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "resetTrainingFinishedFlag failed: ${e.message}")
        }
    }

    /**
     * 学習を即時停止し、すべての状態を学習開始前に戻す完全リセット。
     * - 実行中の学習・アップロード・ポーリングジョブをキャンセル
     * - 保存済みラウンド番号 (training_prefs) をクリア
     * - ExperimentContext (experiment_context SharedPreferences) をクリア
     * - 全 StateFlow を初期値に戻す
     */
    fun fullReset() {
        try {
            RealTimeLogger.i(TAG, "fullReset: initiating full reset")
            // SP1: 自モデルと適用記録を破棄（次の学習はダウンロード済みモデルがあればそこから）
            lastTrainedParams = null
            lastUsedDownloadedSha = null
            // 1. 実行中ジョブをすべて停止
            cancelRunFiveCycles()
            stopMetaPolling()
            // 2. 保存済みラウンドをクリア
            clearSavedRound()
            // 3. ExperimentContext SharedPreferences をクリア
            try {
                val ctx = getApplication<Application>()
                ctx.getSharedPreferences("experiment_context", Context.MODE_PRIVATE)
                    .edit().clear().apply()
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "fullReset: ExperimentContext clear failed: ${e.message}")
            }
            // 4. StateFlow を初期値にリセット
            _trainingProgress.value = 0f
            _trainingFinished.value = false
            _isTrainingRunning.value = false
            _isUploading.value = false
            _uploadProgress.value = 0f
            _uploadAttempt.value = 0
            _errorMessage.value = null
            _trainingDataStatus.value = "リセット完了 — 待機中"
            _isTrainingDataReady.value = false
            _satisfactionBefore.value = null
            _satisfactionAfter.value = null
            _currentPollAttempt.value = 0
            // プログレスバー右下のサイクル/エポック表示をリセット
            _currentCycle.value = 0
            _currentEpoch.value = 0
            // 5. 内部変数をクリア
            lastKnownRound = null
            awaitingModelAfterUpload = false
            fullDataset = emptyList()
            localTrainer = null
            sessionSatisfactionBefore = null
            sessionSatisfactionAfter = null
            sessionAfterSummary = null
            try { _afterN.value = null } catch (_: Exception) {}
            currentSessionId = ""
            // 6. サーバ接続の初回確認状態をリセット（次回起動時に再確認画面が表示される）
            try {
                val ctx = getApplication<Application>()
                ctx.getSharedPreferences("env_prefs", Context.MODE_PRIVATE)
                    .edit()
                    .remove("edge_endpoint_configured")
                    .apply()
                RealTimeLogger.i(TAG, "fullReset: edge_endpoint_configured cleared")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "fullReset: edge_endpoint_configured clear failed: ${e.message}")
            }
            // 7. アプリ種別の割り当てをクリア（再設定画面で再選択可能にする）
            try {
                _assignedAppIndex.value = null
                val ctx = getApplication<Application>()
                ctx.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
                    .edit()
                    .remove("assigned_app_index")
                    .apply()
                RealTimeLogger.i(TAG, "fullReset: assigned_app_index cleared")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "fullReset: assigned_app_index clear failed: ${e.message}")
            }
            RealTimeLogger.i(TAG, "fullReset: complete")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "fullReset failed: ${e.message}")
        }
    }

    @Suppress("unused")
    private fun runNetworkDiagnostics() {
        viewModelScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    networkClient.fetchMeta(forceRefresh = true)
                }
                RealTimeLogger.d(TAG, "runNetworkDiagnostics: meta fetch ok")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "runNetworkDiagnostics failed: ${e.message}")
                _errorMessage.value = "ネットワーク診断に失敗しました: ${e.message}"
            }
        }
    }

    fun notifyExternalModelUpdate(path: String?) {
        try {
            // SP3d 4.1: 受信したことだけを表示・記録する（モデルの取り込みは下の modelUpdateChannel で従来どおり）
            _trainingDataStatus.value = modelReceivedMessage(path)
            try { RealTimeLogger.logEvent("model_received_no_autostart", mapOf("path" to (path ?: ""))) } catch (_: Exception) {}
            // _trainingFinished はここでリセットしない。satisfaction_after 計測中にグレーアウトするため。
            _errorMessage.value = null
            RealTimeLogger.i(TAG, "notifyExternalModelUpdate: path=$path")

            // Try non-suspending send first; if that fails (channel full or closed), fall back to suspending send in a coroutine
            try {
                val res = modelUpdateChannel.trySend(path)
                if (!res.isSuccess) {
                    RealTimeLogger.w(TAG, "notifyExternalModelUpdate: trySend failed (will fallback to suspending send). result=$res")
                    viewModelScope.launch {
                        try {
                            modelUpdateChannel.send(path)
                            RealTimeLogger.i(TAG, "notifyExternalModelUpdate: fallback send succeeded")
                        } catch (e: Exception) {
                            RealTimeLogger.w(TAG, "notifyExternalModelUpdate: fallback send failed: ${e.message}")
                        }
                    }
                } else {
                    RealTimeLogger.i(TAG, "notifyExternalModelUpdate: trySend succeeded")
                }
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "Failed to signal modelUpdateChannel: ${e.message}")
            }

            // SP3d 4.1: 以前はここで、アップロード後に新モデルが届くと runFiveCycles(autoUpload=false) を自動で始めていた
            // （アップロードしないセッションがメトリクスに混ざる）。削除した。awaitingModelAfterUpload はメタのポーリング
            // 間隔の切替に使っているので残す。

            try {
                val cfg = com.example.hfl_experiment.experiment.Config.loadConfigFromAssets(getApplication())
                if (decisionEngine == null) {
                    decisionEngine = com.example.hfl_experiment.experiment.DecisionEngine(getApplication(), cfg, appConfigs = appConfigs)
                }
                RealTimeLogger.i(TAG, "DecisionEngine initialized/refreshed for model update")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "DecisionEngine init failed: ${e.message}")
            }
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "notifyExternalModelUpdate failed: ${e.message}")
        }
    }

    override fun onCleared() {
        try { modelUpdateChannel.close() } catch (e: Exception) { RealTimeLogger.w(TAG, "onCleared: failed to close modelUpdateChannel: ${e.message}") }
        try {
            // Best-effort: flush and close server-style log output so pending bytes hit storage
            try { com.example.hfl_experiment.util.logging.ServerStyleLogger.flush() } catch (_: Throwable) {}
            try { com.example.hfl_experiment.util.logging.ServerStyleLogger.close() } catch (_: Throwable) {}
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "onCleared: failed to flush/close ServerStyleLogger: ${e.message}")
        }
        super.onCleared()
    }

    // Persist ModelParameters as a simple JSON file in app filesDir. This is a best-effort helper used after uploads
    private fun writeModelParametersToFile(p: ModelParameters, filename: String) {
        try {
            val app = getApplication<Application>()
            val outFile = java.io.File(app.filesDir, filename)
            val root = JSONObject()
            fun floatListToJsonArray(list: List<Float>): JSONArray {
                val a = JSONArray()
                for (v in list) a.put(v.toDouble())
                return a
            }
            // weights1 assumed to be List<List<Float>>
            try {
                val w1 = JSONArray()
                for (row in p.weights1) {
                    val r = JSONArray()
                    for (v in row) r.put(v.toDouble())
                    w1.put(r)
                }
                root.put("weights1", w1)
            } catch (_: Exception) {}

            try { root.put("biases1", floatListToJsonArray(p.biases1)) } catch (_: Exception) {}
            try { root.put("norm1Gamma", floatListToJsonArray(p.norm1Gamma)) } catch (_: Exception) {}
            try { root.put("norm1Beta", floatListToJsonArray(p.norm1Beta)) } catch (_: Exception) {}

            try {
                val w2 = JSONArray()
                for (row in p.weights2) {
                    val r = JSONArray()
                    for (v in row) r.put(v.toDouble())
                    w2.put(r)
                }
                root.put("weights2", w2)
            } catch (_: Exception) {}
            try { root.put("biases2", floatListToJsonArray(p.biases2)) } catch (_: Exception) {}
            try { root.put("norm2Gamma", floatListToJsonArray(p.norm2Gamma)) } catch (_: Exception) {}
            try { root.put("norm2Beta", floatListToJsonArray(p.norm2Beta)) } catch (_: Exception) {}

            try {
                val w3 = JSONArray()
                for (row in p.weights3) {
                    val r = JSONArray()
                    for (v in row) r.put(v.toDouble())
                    w3.put(r)
                }
                root.put("weights3", w3)
            } catch (_: Exception) {}
            try { root.put("biases3", floatListToJsonArray(p.biases3)) } catch (_: Exception) {}

            outFile.writeText(root.toString())
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "writeModelParametersToFile failed: ${e.message}")
            throw e
        }
    }

    private suspend fun runBaselineMeasurements(traceSessionCookie: Int): Boolean {
        if (decisionEngine == null) {
            try {
                val cfg = com.example.hfl_experiment.experiment.Config.loadConfigFromAssets(getApplication())
                decisionEngine = com.example.hfl_experiment.experiment.DecisionEngine(getApplication(), cfg, appConfigs = appConfigs)
                RealTimeLogger.i(TAG, "runBaselineMeasurements: DecisionEngine initialized")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "runBaselineMeasurements: DecisionEngine init failed: ${e.message}")
            }
        }

        val baselineCfg = try {
            com.example.hfl_experiment.experiment.Config.loadConfigFromAssets(getApplication())
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "runBaselineMeasurements: AP config load failed: ${e.message}")
            null
        }

        // SP3d: 計測回数 = セッションのサイクル数 × 5（HFL のサイクル×エポックに対応。以前は 100 回固定）。間隔 3 秒は同じ
        val totalMeasurements = baselineMeasurementCount(AppConfig.numCycles)
        _totalCycles.value = AppConfig.numCycles
        greedySourceCounts.clear()
        decisionEngine?.resetSessionRandomChoice()   // fix-audit-20261026 N3: Random はこのセッションの最初の計測で 1 回だけ決める
        val baselineSamples = mutableListOf<Double?>()   // SP3d 3.2: 要約行に入れる各回の満足度（失敗は null）
        // centralized-mode: central は学習せず、サイクルごとに中央へ問い合わせて推奨 AP を記録するだけ（AP は自動では切り替えない）
        val sessionMode = AppConfig.BASELINE_MODE
        val isCentral = sessionMode == com.example.hfl_experiment.experiment.CentralSessionPolicy.MODE
        val isHflRec = sessionMode == com.example.hfl_experiment.experiment.CentralSessionPolicy.HFL_REC_MODE   // hfl-recommended-ap-mode
        val isQueryMode = com.example.hfl_experiment.experiment.CentralSessionPolicy.isQueryMode(sessionMode)
        val centralUrl = if (isQueryMode) AppConfig.getCentralUrl(getApplication()) else ""
        val centralClient = if (isQueryMode) com.example.hfl_experiment.experiment.CentralClient(
            centralUrl, path = com.example.hfl_experiment.experiment.CentralSessionPolicy.queryPath(sessionMode)) else null
        // hfl_rec: observe() が毎計測で lastDecisionResult を上書きするので、提案は問い合わせの結果（成功したときだけ）から出す
        var hflRecDecision: com.example.hfl_experiment.experiment.DecisionResult? = null
        val centralRecords = mutableListOf<com.example.hfl_experiment.experiment.CentralCycleRecord>()
        for (measurement in 1..totalMeasurements) {
            val (currentCycle, currentEpoch) = baselineCycleEpoch(measurement)
            _currentCycle.value = currentCycle
            _currentEpoch.value = currentEpoch
            withContext(Dispatchers.Main) {
                _trainingDataStatus.value = "ベースライン ${AppConfig.BASELINE_MODE}: サイクル $currentCycle エポック $currentEpoch"
            }

            if (AppConfig.BASELINE_MODE == "greedy") {
                try {
                    // record-connected-ap: 実測（measured）つきで取り、Greedy は実測優先で比べる
                    val apInfos = networkClient.fetchApStateInfos()
                    if (apInfos != null) {
                        decisionEngine?.apStateInfosForGreedy = apInfos.entries.associate { (key, value) ->
                            (key.removePrefix("AP").toIntOrNull() ?: 0) to value
                        }
                    }
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "runBaselineMeasurements: fetchApStates failed: ${e.message}")
                }
            }

            val sat = measureAndLogTerminalSatisfaction(null, label = "baseline_c${currentCycle}_e${currentEpoch}")
            baselineSamples.add(sat?.toDouble())
            RealTimeLogger.i(TAG, "runBaselineMeasurements: cycle=$currentCycle epoch=$currentEpoch sat=$sat mode=${AppConfig.BASELINE_MODE}")

            if (centralClient != null && com.example.hfl_experiment.experiment.CentralSessionPolicy.queryDue(sessionMode, currentEpoch)) {
                val appType = resolveAppType(appConfigs?.getOrNull(_assignedAppIndex.value ?: 0)?.appType)
                val outcome = withContext(Dispatchers.IO) { centralClient.query(terminalId, appType) }
                centralRecords.add(com.example.hfl_experiment.experiment.CentralCycleRecord(currentCycle, outcome))
                lastDecisionResult = decisionEngine?.observeCentral(outcome)   // 記録用。端末は推奨 AP を自動では適用しない
                if (isHflRec && outcome.status == com.example.hfl_experiment.experiment.CentralClient.STATUS_OK) hflRecDecision = lastDecisionResult
                RealTimeLogger.i(TAG, "runBaselineMeasurements: central cycle=$currentCycle status=${outcome.status} rttMs=${outcome.rttMs} ap=${outcome.chosenAp} sha=${outcome.modelSha256?.take(12)}")
            }

            try {
                // central は推奨 AP を記録するだけ（AP の切替は実験者が手で行う）
                val decision = when {
                    isHflRec -> hflRecDecision   // 問い合わせが成功するまでは提案しない（失敗時は現状維持）
                    com.example.hfl_experiment.experiment.CentralSessionPolicy.shouldProposeApSwitch(sessionMode) -> lastDecisionResult
                    else -> null
                }
                if (decision != null && baselineCfg != null && decision.chosenApId.isNotBlank()) {
                    val chosenIdx = if (decision.chosenApId == baselineCfg.apA.id) 0 else 1
                    val targetSsid = if (chosenIdx == 0) AppConfig.getPrimarySsid(getApplication()) else AppConfig.getSecondarySsid(getApplication())
                    val targetPass = if (chosenIdx == 0) AppConfig.ROUTER_PASS_A else AppConfig.ROUTER_PASS_B
                    val currentSsid = com.example.hfl_experiment.util.networking.NetworkSwitchHelper.getConnectedSsid(getApplication())
                    if (!targetSsid.isNullOrBlank() && currentSsid != targetSsid) {
                        val status = com.example.hfl_experiment.util.networking.NetworkSwitchHelper.suggestWifi(
                            getApplication(), targetSsid, targetPass.takeIf { it.isNotBlank() }
                        )
                        RealTimeLogger.i(TAG, "runBaselineMeasurements: suggestWifi ssid=$targetSsid status=$status chosen=${decision.chosenApId} idx=$chosenIdx")
                        SwitchTarget.ssid = targetSsid  // SP5: 無人モードの After はこの AP に繋がるのを待つ
                        // record-connected-ap: 提案の時点では ap_states/connect を送らない。以前はここで「提案した AP に移った」と通知し、
                        // 手動切替の運用では実際には移っていないのに端末の AP を上書きしていた（B にいる端末が AP0 と数えられ、実測も消えた）。
                        // 実際に繋がっている AP は、計測のたびに送る observe（postApObservation）が運ぶ。
                    } else {
                        RealTimeLogger.d(TAG, "runBaselineMeasurements: already on $targetSsid, no switch needed")
                    }
                }
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "runBaselineMeasurements: AP switch suggestion failed: ${e.message}")
            }

            _trainingProgress.value = measurement.toFloat() / totalMeasurements.toFloat()
            kotlinx.coroutines.delay(3000L)
        }

        withContext(Dispatchers.Main) {
            _trainingProgress.value = 1.0f
            _trainingFinished.value = true
            _trainingDataStatus.value = "ベースライン計測完了 (${AppConfig.BASELINE_MODE}, $totalMeasurements 回) -> アップロード中..."
        }
        try { saveExperimentRecord() } catch (_: Exception) {}
        // SP3d 3.2: セッション要約行を台帳・未送信キューで中央へ送る（After はこの行に同じ data_id で補完される）
        try {
            val appIdx = _assignedAppIndex.value ?: 0
            val row = buildBaselineSessionRow(
                BaselineSessionInfo(
                    runId = currentSessionId,
                    terminalId = AppConfig.getTerminalId(getApplication()),
                    experimentGroup = com.example.hfl_experiment.experiment.ExperimentContext.experimentGroup.ifEmpty { null },
                    baselineMode = AppConfig.BASELINE_MODE,
                    baselineModeSource = AppConfig.baselineModeSource.value,
                    hflMode = AppConfig.hflMode,
                    hflModeSource = AppConfig.hflModeSource.value,
                    numCycles = AppConfig.numCycles,
                    numCyclesSource = AppConfig.numCyclesSource.value,
                    plannedMeasurements = totalMeasurements,
                    appType = resolveAppType(appConfigs?.getOrNull(appIdx)?.appType),
                    appIndex = appIdx,
                    virtualRouterId = currentRouterId,
                    timestampMs = System.currentTimeMillis(),
                ),
                satisfactionBefore = sessionSatisfactionBefore?.toDouble(),
                samples = baselineSamples,
                central = when {
                    isCentral -> com.example.hfl_experiment.experiment.centralSessionFields(centralRecords, centralUrl)
                    isHflRec -> com.example.hfl_experiment.experiment.hflRecSessionFields(centralRecords, centralUrl)
                    else -> randomSessionFields(sessionMode, decisionEngine?.sessionRandomChoice)   // random のときだけ random_choice_ap
                },
            )
            // After の補完（measureSatisfactionAfter）で followed を求めるため、このセッションの問い合わせ結果を覚える
            hflRecSession = if (isHflRec) row.optString("hfl_rec_status") to (if (row.isNull("hfl_rec_ap")) null else row.optInt("hfl_rec_ap")) else null
            putConnectedAp(row, readConnectedAp())  // record-connected-ap
            if (AppConfig.BASELINE_MODE == "greedy") row.put("greedy_ap_sources", JSONObject(greedySourceCounts.toMap()))
            recordAndSend(row)
            RealTimeLogger.i(TAG, "runBaselineMeasurements: baseline_session row data_id=${row.optString("data_id")} n=${row.optInt("satisfaction_baseline_n")}")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "runBaselineMeasurements: baseline_session row failed: ${e.message}")
        }
        if (com.example.hfl_experiment.experiment.CentralSessionPolicy.shouldUploadAtEnd(sessionMode)) {
            try {
                RealTimeLogger.i(TAG, "runBaselineMeasurements: triggering auto-upload of logs")
                uploadUpdate()
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "runBaselineMeasurements: auto-upload failed: ${e.message}")
            }
        } else {
            RealTimeLogger.i(TAG, "runBaselineMeasurements: $sessionMode — 学習も重みの送信もしないので自動アップロードは行わない")
        }
        com.example.hfl_experiment.telemetry.HflTracer.endAsync("HFL_runFiveCycles", traceSessionCookie)
        return true
    }

    private suspend fun runTrainingCycles(
        inputSize: Int,
        hiddenSize: Int,
        outputSize: Int,
        epochs: Int,
        autoUpload: Boolean,
        initialParams: ModelParameters?,
        traceSessionCookie: Int
    ): Boolean {
        val cycles = AppConfig.numCycles   // SP3d: セッション開始時に固定した値
        _totalCycles.value = cycles
        _epochsPerCycle.value = epochs
        // 問題7: 手元に配布モデルが無い（新規インストール直後など）と cycle1 がランダム初期値になっていた
        // （配布モデルを取りに行くのは cycle1 のアップロード後だった）。cycle1 の前に最新の配布モデルを 1 回取り込む。
        // 取れなければ従来どおり（手元のモデル → 自モデル → ランダム）。使ったかどうかは init_source に残る。
        // あわせて現在のラウンドを取る（cycle1 のメトリクス行の round が NULL だった。cycle2 以降は進行待ちで更新される）。
        try {
            val meta = withTimeoutOrNull(AppConfig.SESSION_START_MODEL_FETCH_TIMEOUT_MS) {
                networkClient.fetchMeta(forceRefresh = true, ignoreThrottle = true)
            }
            if (meta != null) {
                lastKnownRound = meta.round
                try { persistLastKnownRound(meta.round) } catch (_: Exception) {}
                try { com.example.hfl_experiment.experiment.ExperimentContext.updateRound(getApplication(), meta.round) } catch (_: Exception) {}
                try { _serverRound.value = meta.round } catch (_: Exception) {}
            }
            RealTimeLogger.i(TAG, "runTrainingCycles: session_start meta round=${meta?.round ?: "unavailable"}")
        } catch (ce: CancellationException) { throw ce } catch (e: Exception) {
            RealTimeLogger.w(TAG, "runTrainingCycles: session_start meta fetch failed: ${e.message}")
        }
        try {
            val fetched = withTimeoutOrNull(AppConfig.SESSION_START_MODEL_FETCH_TIMEOUT_MS) {
                fetchAndApplyLatestGlobalModel("session_start")
            }
            RealTimeLogger.i(TAG, "runTrainingCycles: session_start global model fetch result=${fetched ?: "timeout"}")
        } catch (ce: CancellationException) { throw ce } catch (e: Exception) {
            RealTimeLogger.w(TAG, "runTrainingCycles: session_start global model fetch failed: ${e.message}")
        }
        for (cycle in 1..cycles) {
            var traceCycleCookie = 0
            try {
                _currentCycle.value = cycle
                _currentEpoch.value = 0
                withContext(Dispatchers.Main) { _trainingDataStatus.value = "学習サイクル ${cycle}/$cycles 実行中..." }
                traceCycleCookie = com.example.hfl_experiment.telemetry.HflTracer.beginAsync("HFL_cycle_$cycle")

                val initParamsForThisCycle = if (cycle == 1) initialParams else null

                if (cycle == 1) {
                    try { com.example.hfl_experiment.util.logging.RealTimeLogger.trainingStart(epochs) } catch (_: Exception) {}
                }

                val ok = performLocalTrainingSuspend(
                    inputSize = inputSize,
                    hiddenSize = hiddenSize,
                    outputSize = outputSize,
                    epochs = epochs,
                    onProgress = { pf ->
                        try {
                            val globalProgress = ((cycle - 1).toFloat() + pf) / cycles.toFloat()
                            try { viewModelScope.launch(Dispatchers.Main) { _trainingProgress.value = globalProgress } } catch (_: Exception) {}
                            try { TrainingSimpleLogger.appendProgress(globalProgress) } catch (_: Exception) {}
                        } catch (e: Exception) {
                            RealTimeLogger.w(TAG, "onProgress callback failed: ${e.message}")
                        }
                    },
                    onEpoch = { e ->
                        try { _currentEpoch.value = e } catch (_: Exception) {}
                    },
                    initialParams = initParamsForThisCycle
                )

                if (!ok) {
                    RealTimeLogger.w(TAG, "runTrainingCycles: local training failed at cycle=$cycle")
                    withContext(Dispatchers.Main) { _errorMessage.value = "ローカル学習が失敗しました（cycle=$cycle）" }
                    try { com.example.hfl_experiment.util.logging.RealTimeLogger.anomaly("local_training_failed", "cycle=$cycle") } catch (_: Exception) {}
                    return false
                }

                try { persistTrainingMetrics(fullDataset.size) } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "runTrainingCycles: persistTrainingMetrics failed: ${e.message}")
                }
                // SP-M: このサイクルの行（学習側の値）を作り台帳へ。アップロードに同梱し、アップロード後に補完して送る
                currentCycleRow = try { buildCycleRow() } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "runTrainingCycles: buildCycleRow failed: ${e.message}"); null
                }
                currentCycleRow?.let { TrainingMetricsStore.init(getApplication()); TrainingMetricsStore.recordRow(it) }

                try { lastUploadedModelParameters = localTrainer?.getModelParameters() } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "runTrainingCycles: getModelParameters failed: ${e.message}")
                }

                if (autoUpload) {
                    try {
                        withContext(Dispatchers.IO) {
                            try { com.example.hfl_experiment.util.logging.RealTimeLogger.uploadStart(AppConfig.getEdgeBaseUrl(getApplication())) } catch (_: Exception) {}
                            val uploadOk = try {
                                // 手動アップロード(uploadSlot)と排他
                                val res = uploadSlot.mutex.withLock { uploadUpdateSuspend() }
                                RealTimeLogger.i(TAG, "runTrainingCycles: uploadUpdateSuspend returned $res for cycle=$cycle")
                                res
                            } catch (ce: CancellationException) {
                                RealTimeLogger.i(TAG, "runTrainingCycles: upload cancelled during cycle=$cycle")
                                throw ce
                            } catch (e: Exception) {
                                RealTimeLogger.e(TAG, "runTrainingCycles: uploadUpdateSuspend threw for cycle=$cycle: ${e.message}", e)
                                false
                            }
                            try { com.example.hfl_experiment.util.logging.RealTimeLogger.uploadEnd(AppConfig.getEdgeBaseUrl(getApplication()), uploadOk, null) } catch (_: Exception) {}

                            var cycleSat: Float? = null
                            try {
                                try {
                                    RealTimeLogger.i(TAG, "runTrainingCycles: forcing satisfaction measurement before telemetry send (cycle=$cycle)")
                                    try { cycleSat = measureAndLogTerminalSatisfaction(null, label = "cycle_${cycle}") } catch (e: Exception) {
                                        RealTimeLogger.w(TAG, "force satisfaction measurement failed: ${e.message}")
                                    }
                                    try { kotlinx.coroutines.delay(500) } catch (_: Exception) {}
                                } catch (e: Exception) {
                                    RealTimeLogger.w(TAG, "runTrainingCycles: measurement step failed: ${e.message}")
                                }
                                val sent = TelemetrySender.send(getApplication())
                                RealTimeLogger.i(TAG, "runTrainingCycles: TelemetrySender.send returned=$sent for cycle=$cycle")
                            } catch (e: Exception) {
                                RealTimeLogger.w(TAG, "runTrainingCycles: Telemetry send failed for cycle=$cycle: ${e.message}")
                            }

                            // SP-M: アップロード結果・当該サイクルの満足度/TP/RTT を同じ data_id の行に補完して自動送信（失敗時も）
                            currentCycleRow?.let { row ->
                                recordAndSend(completeCycleMetric(
                                    row,
                                    UploadOutcome(uploadOk, lastUploadPostMs, lastNetworkTransport),
                                    cycleSat?.toDouble(), lastBandwidthMbps, lastMeasuredRttMs?.toDouble()
                                ))
                            }

                            if (!uploadOk) {
                                withContext(Dispatchers.Main) { _errorMessage.value = "アップロードに失敗しました（cycle=$cycle）" }
                                throw Exception("upload failed at cycle=$cycle")
                            }

                            val prevRound = uploadedRoundAfterUpload ?: lastKnownRound
                            try {
                                if (AppConfig.isAsyncMode()) {
                                    // 非同期モード（async-hfl 由来）: ラウンド進行は最大 ASYNC_ROUND_WAIT_TIMEOUT_MS だけ待ち、
                                    // 来なくても次サイクルへ。届いている最新グローバルがあれば取り込む（未使用のものだけ使われる, F8）
                                    val asyncStart = System.currentTimeMillis()
                                    var advanced = false
                                    try {
                                        withTimeout(AppConfig.ASYNC_ROUND_WAIT_TIMEOUT_MS) {
                                            advanced = waitForServerRoundToAdvance(
                                                prevRound,
                                                statusText = "Async: ラウンド確認（最大 ${AppConfig.ASYNC_ROUND_WAIT_TIMEOUT_MS / 1000} 秒）"
                                            )
                                        }
                                    } catch (_: kotlinx.coroutines.TimeoutCancellationException) {
                                        advanced = false
                                    }
                                    val elapsed = System.currentTimeMillis() - asyncStart
                                    RealTimeLogger.i(TAG, "runTrainingCycles[async]: round_check advanced=$advanced elapsed=${elapsed}ms cycle=$cycle — continue regardless")
                                    if (cycle < cycles) {
                                        try { fetchAndApplyLatestGlobalModel("async_cycle_$cycle") } catch (ce: CancellationException) { throw ce } catch (e: Exception) {
                                            RealTimeLogger.w(TAG, "runTrainingCycles[async]: global model fetch failed: ${e.message}")
                                        }
                                    }
                                } else {
                                    val waited = waitForServerRoundToAdvance(prevRound)
                                    if (!waited) {
                                        RealTimeLogger.w(TAG, "runTrainingCycles: waitForServerRoundToAdvance returned false (cycle=$cycle)")
                                    } else {
                                        // 新しいグローバルモデルを取り込んでから次サイクルへ（取れなければ自モデルで継続, F8）
                                        if (cycle < cycles) {
                                            try { fetchAndApplyLatestGlobalModel("round_advanced_cycle_$cycle") } catch (ce: CancellationException) { throw ce } catch (e: Exception) {
                                                RealTimeLogger.w(TAG, "runTrainingCycles: global model fetch failed: ${e.message}")
                                            }
                                        }
                                        RealTimeLogger.i(TAG, "runTrainingCycles: server round advanced — continuing to next cycle (cycle=$cycle)")
                                    }
                                }
                            } catch (ce: CancellationException) {
                                RealTimeLogger.i(TAG, "runTrainingCycles: wait cancelled while waiting for server round (cycle=$cycle)")
                                throw ce
                            } catch (e: Exception) {
                                RealTimeLogger.w(TAG, "runTrainingCycles: waitForServerRoundToAdvance failed: ${e.message}")
                            }
                        }
                    } catch (ce: CancellationException) {
                        RealTimeLogger.i(TAG, "runTrainingCycles: cancelled during upload/wait at cycle=$cycle")
                        throw ce
                    } catch (e: Exception) {
                        RealTimeLogger.w(TAG, "runTrainingCycles: aborting due to upload/wait failure at cycle=$cycle: ${e.message}")
                        return false
                    }
                }

                if (!autoUpload) currentCycleRow?.let { recordAndSend(it) }

                withContext(Dispatchers.Main) {
                    _trainingProgress.value = (cycle.toFloat() / cycles.toFloat())
                }
                com.example.hfl_experiment.telemetry.HflTracer.endAsync("HFL_cycle_$cycle", traceCycleCookie)

            } catch (e: CancellationException) {
                com.example.hfl_experiment.telemetry.HflTracer.endAsync("HFL_cycle_$cycle", traceCycleCookie)
                RealTimeLogger.i(TAG, "runTrainingCycles cancelled at cycle=$cycle")
                throw e
            } catch (e: Exception) {
                com.example.hfl_experiment.telemetry.HflTracer.endAsync("HFL_cycle_$cycle", traceCycleCookie)
                RealTimeLogger.e(TAG, "runTrainingCycles: exception during cycle=$cycle", e)
                withContext(Dispatchers.Main) { _errorMessage.value = "学習中に例外が発生しました: ${e.message}" }
                return false
            }
        }
        // 全サイクル正常完了 → 自動再起動フラグを解除（satisfaction_after 計測ウィンドウを確保するため, async-hfl 由来）
        awaitingModelAfterUpload = false
        withContext(Dispatchers.Main) {
            _trainingProgress.value = 1.0f
            _trainingFinished.value = true
            _trainingDataStatus.value = cyclesCompletedMessage(cycles)
        }
        try { saveExperimentRecord() } catch (e: Exception) {
            RealTimeLogger.w(TAG, "runTrainingCycles: final saveExperimentRecord failed: ${e.message}")
        }
        com.example.hfl_experiment.telemetry.HflTracer.endAsync("HFL_runFiveCycles", traceSessionCookie)
        com.example.hfl_experiment.telemetry.HflTracer.collectUsageStatsAsync(getApplication())
        try {
            com.example.hfl_experiment.telemetry.DeviceMetricsCollector.persist(
                sessionId = currentSessionId, round = lastKnownRound ?: 0
            )
        } catch (_: Exception) {}
        return true
    }

    /**
     * 実験セッション全体のデータを1つのJSONファイルに書き出す。
     * - 学習開始時に satisfaction_before を含めて生成
     * - measureSatisfactionAfter() 呼び出し時に satisfaction_after を更新
     * - runFiveCycles() 完了時に最終更新
     *
     * ファイル名: experiment_SESSION_ID.json
     */
    private suspend fun saveExperimentRecord() {
        withContext(Dispatchers.IO) {
            try {
                val appIdx = _assignedAppIndex.value ?: 0
                val jo = JSONObject()
                jo.put("session_id", currentSessionId)  // 後方互換のため残す
                jo.put("run_id", currentSessionId)       // 新スキーマと統一
                jo.put("timestamp_ms", System.currentTimeMillis())
                jo.put("terminal_id", terminalId)
                jo.put("app_index", appIdx)
                jo.put("app_type", appConfigs?.getOrNull(appIdx)?.appType ?: "unknown")
                jo.put("satisfaction_before", sessionSatisfactionBefore ?: JSONObject.NULL)
                jo.put("satisfaction_after", sessionSatisfactionAfter ?: JSONObject.NULL)
                putAfterSummary(jo, sessionAfterSummary)
                jo.put("round", lastKnownRound ?: JSONObject.NULL)
                jo.put("virtual_router_id", currentRouterId)
                jo.put("num_cycles", AppConfig.numCycles)
                jo.put("num_cycles_source", AppConfig.numCyclesSource.value)

                // Training summary
                val avgValLoss = epochMetrics.mapNotNull { it.valLoss }.let { if (it.isNotEmpty()) it.average() else null }
                val avgValAcc = epochMetrics.mapNotNull { it.valAcc }.let { if (it.isNotEmpty()) it.average() else null }
                jo.put("total_training_ms", lastLocalTrainingTotalMs)
                jo.put("n_samples", fullDataset.size)
                if (avgValLoss != null) jo.put("avg_val_loss", avgValLoss)
                if (avgValAcc != null) jo.put("avg_val_acc", avgValAcc)
                jo.put("battery_before_pct", batteryLevelBeforeTraining ?: JSONObject.NULL)
                jo.put("battery_after_pct", batteryLevelAfterTraining ?: JSONObject.NULL)
                jo.put("network_transport", lastNetworkTransport ?: JSONObject.NULL)
                jo.put("uploaded_weights_sha256", lastUploadedWeightsSha256 ?: JSONObject.NULL)

                val filesDir = getApplication<Application>().filesDir
                File(filesDir, "experiment_${currentSessionId}.json").writeText(jo.toString())
                // JSONL 台帳へ追記（最大 500KB を超えたら古い行を先頭から削除）
                val histFile = File(filesDir, "experiment_history.jsonl")
                histFile.appendText(jo.toString() + "\n")
                if (histFile.length() > 500 * 1024) {
                    try {
                        val lines = histFile.readLines()
                        val keep = lines.takeLast((lines.size * 0.8).toInt().coerceAtLeast(1))
                        histFile.writeText(keep.joinToString("\n") + "\n")
                    } catch (_: Exception) {}
                }

                RealTimeLogger.i(TAG, "saveExperimentRecord: wrote experiment_${currentSessionId}.json")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "saveExperimentRecord failed: ${e.message}")
            }
        }
    }

    // Run five sequential local training cycles and optionally upload the aggregated update
    @Suppress("MemberVisibilityCanBePrivate")
    suspend fun runFiveCycles(
        inputSize: Int,
        hiddenSize: Int,
        outputSize: Int,
        epochs: Int = 1,
        autoUpload: Boolean = true,
        initialParams: ModelParameters? = null
    ): Boolean {
        if (afterMeasuring.get()) {
            // SP3d: After の計測中は学習を始めない（計測中の AP・満足度に学習が重ならないように）
            _errorMessage.value = "満足度(After)の計測中です。終わってから学習を開始してください。"
            RealTimeLogger.w(TAG, "runFiveCycles: refused while measuring satisfaction_after")
            return false
        }
        if (!isTrainingDataReady.value || fullDataset.isEmpty()) {
            _errorMessage.value = "学習データが空であるか、準備できていません。"
            RealTimeLogger.w(TAG, "runFiveCycles: fullDataset is empty or not ready.")
            return false
        }

        // register current coroutine Job so external callers can cancel via cancelRunFiveCycles
        currentRunJob = currentCoroutineContext()[Job]
        withContext(Dispatchers.Main) { _isTrainingRunning.value = true; _trainingFinished.value = false }

        // Perfetto async trace for the entire 5-cycle training session
        val traceSessionCookie = com.example.hfl_experiment.telemetry.HflTracer.beginAsync("HFL_runFiveCycles")

        // SP-B: 最新のサーバ指定を取得し、このセッションのベースラインモードを決めて固定する（サーバ優先）
        try { networkClient.fetchMeta(forceRefresh = true, ignoreThrottle = true) } catch (e: Exception) {
            RealTimeLogger.w(TAG, "runFiveCycles: fetchMeta for baseline mode failed: ${e.message} (using last known)")
        }
        val (prevBaselineMode, sessionBaselineMode) = AppConfig.beginSessionBaselineMode(getApplication())
        // SP3b R1: 同じ /meta から HFL モード（sync/async）も決めて固定する（サーバ優先、無ければアプリ設定）
        val (sessionHflMode, hflModeSource, hflMismatchApp) = AppConfig.beginSessionHflMode()
        RealTimeLogger.i(TAG, "runFiveCycles: hfl_mode=$sessionHflMode source=$hflModeSource app=${AppConfig.appHflMode}")
        // SP3d: 同じ /meta からサイクル数も決めて固定する（サーバ優先、無ければアプリ設定）。変化しても切替手順は行わない
        val (sessionCycles, cyclesSource) = AppConfig.beginSessionNumCycles()
        _totalCycles.value = sessionCycles
        RealTimeLogger.i(TAG, "runFiveCycles: num_cycles=$sessionCycles source=$cyclesSource app=${AppConfig.appNumCycles}")
        RealTimeLogger.i(TAG, "runFiveCycles: baseline_mode=$sessionBaselineMode source=${AppConfig.baselineModeSource.value} (prev=$prevBaselineMode)")
        if (prevBaselineMode != sessionBaselineMode) {
            // モードが変わるときは、前セッションのデータを確定・送達してから切り替える（唐突に切り替えない）
            withContext(Dispatchers.Main) { _trainingDataStatus.value = "モード切替前に前回のデータを保存しています…（$prevBaselineMode → $sessionBaselineMode）" }
            val app = getApplication<Application>()
            val prevRunId = currentSessionId
            val switchClient = com.example.hfl_experiment.network.NetworkClient(app, AppConfig.getEdgeBaseUrl(app), authToken = AppConfig.getServerAuthToken(app))
            TrainingMetricsStore.init(app)
            val undelivered = runModeSwitchProcedure(
                finalize = {
                    if (prevRunId.isNotEmpty()) saveExperimentRecord()
                    try { RealTimeLogger.logEvent("session_end", mapOf("run_id" to prevRunId, "reason" to "baseline_mode_change")) } catch (_: Exception) {}
                },
                deliver = {
                    try { com.example.hfl_experiment.work.UploadLogsWorker.enqueueOnce(app) } catch (_: Exception) {}
                    com.example.hfl_experiment.worker.UploadMetricsWorker.drainOutbox { row -> switchClient.sendRowJson(row) }
                },
                pendingCount = { TrainingMetricsStore.pending().count { it.optString("kind") != "experiment_event" } },
                record = { n ->
                    val ev = JSONObject().put("kind", "experiment_event").put("type", "baseline_mode_change")
                        .put("terminal_id", terminalId).put("from", prevBaselineMode).put("to", sessionBaselineMode)
                        .put("source", AppConfig.baselineModeSource.value).put("prev_run_id", prevRunId)
                        .put("undelivered", n).put("timestamp_ms", System.currentTimeMillis())
                    try {
                        RealTimeLogger.logEvent("baseline_mode_change", mapOf(
                            "from" to prevBaselineMode, "to" to sessionBaselineMode,
                            "source" to AppConfig.baselineModeSource.value, "prev_run_id" to prevRunId, "undelivered" to n.toString()))
                    } catch (_: Exception) {}
                    TrainingMetricsStore.recordRow(ev)
                    com.example.hfl_experiment.worker.UploadMetricsWorker.enqueue(app)
                },
            )
            RealTimeLogger.i(TAG, "runFiveCycles: baseline mode switched $prevBaselineMode -> $sessionBaselineMode (undelivered=$undelivered)")
        }

        // ExperimentContext に run_id を発行し、ログ・HTTP ヘッダ・フォームに自動反映させる
        currentSessionId = com.example.hfl_experiment.experiment.ExperimentContext
            .startNewRun(getApplication())
        sessionStartedAtMs = System.currentTimeMillis()
        SwitchTarget.ssid = null
        if (hflMismatchApp != null) {
            // 端末設定とサーバの食い違い: サーバに従い、セッションごとに1回実験イベントとして残す
            recordHflModeMismatch(app = hflMismatchApp, server = sessionHflMode)
        }
        sessionSatisfactionBefore = null
        sessionSatisfactionAfter = null
        sessionAfterSummary = null
        try { _satisfactionBefore.value = null } catch (_: Exception) {}
        try { _satisfactionAfter.value = null } catch (_: Exception) {}
        try { _afterN.value = null } catch (_: Exception) {}

        // Initialize device metrics collector for this session
        try {
            com.example.hfl_experiment.telemetry.DeviceMetricsCollector.init(getApplication())
            com.example.hfl_experiment.telemetry.DeviceMetricsCollector.resetForSession(sessionCycles * epochs)
        } catch (_: Exception) {}

        // Measure satisfaction BEFORE training starts — this is the baseline
        try {
            withContext(Dispatchers.Main) { _trainingDataStatus.value = "学習前の端末満足度を計測中..." }
            val sb = measureAndLogTerminalSatisfaction(null, label = "before")
            sessionSatisfactionBefore = sb
            try { _satisfactionBefore.value = sb } catch (_: Exception) {}
            RealTimeLogger.i(TAG, "runFiveCycles: satisfaction_before=${sb} session=${currentSessionId}")
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "runFiveCycles: satisfaction_before measurement failed: ${e.message}")
        }

        // Write initial experiment record (before training, satisfaction_after will be null until AP switch)
        try { saveExperimentRecord() } catch (e: Exception) {
            RealTimeLogger.w(TAG, "runFiveCycles: initial saveExperimentRecord failed: ${e.message}")
        }

        // SP5: セッションの結果（session_complete 用）
        var sessionResult = "error"
        var sessionReason: String? = null
        try {
            val ok = if (AppConfig.BASELINE_MODE != "none") {
                RealTimeLogger.i(TAG, "runFiveCycles: BASELINE MODE (${AppConfig.BASELINE_MODE}) — skipping training")
                runBaselineMeasurements(traceSessionCookie)
            } else {
                runTrainingCycles(inputSize, hiddenSize, outputSize, epochs, autoUpload, initialParams, traceSessionCookie)
            }
            sessionResult = if (ok) "ok" else "error"
            if (!ok) sessionReason = "session_returned_false"
            return ok
        } catch (ce: CancellationException) {
            sessionResult = "cancelled"
            throw ce
        } catch (e: Exception) {
            sessionReason = "exception: ${e.message}".take(200)
            throw e
        } finally {
            // endSession* の前に、このセッションで固定した設定を控える。無人モードは After の後に送る（finishUnattendedSession）
            try {
                val info = sessionInfoNow(if (unattended) unattendedRunTag else null, sessionResult, sessionReason, sessionStartedAtMs)
                if (unattended) pendingSessionComplete = info else sendSessionComplete(info)
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "session_complete snapshot failed: ${e.message}")
            }
            try { AppConfig.endSessionBaselineMode() } catch (_: Exception) {}
            try { AppConfig.endSessionHflMode() } catch (_: Exception) {}
            try { AppConfig.endSessionNumCycles() } catch (_: Exception) {}
            try { currentRunJob = null } catch (_: Exception) {}
            withContext(Dispatchers.Main) { _isTrainingRunning.value = false }
        }
    }

    // Poll /api/v1/meta until server round > previousRound. Default polling interval is 5000ms.
    private suspend fun waitForServerRoundToAdvance(
        previousRound: Int?,
        pollIntervalMs: Long = 5000L,
        maxWaitMs: Long = 10 * 60 * 1000L,  // デフォルト最大10分で待機を打ち切る
        statusText: String? = null          // 画面表示の上書き（Async では「最大10分」と出さない, R6）
    ): Boolean {
        val base = previousRound ?: lastKnownRound ?: 0
        val deadline = System.currentTimeMillis() + maxWaitMs
        RealTimeLogger.i(TAG, "waitForServerRoundToAdvance: waiting for round > $base (poll=${pollIntervalMs}ms maxWait=${maxWaitMs}ms)")
        try {
            withContext(Dispatchers.Main) {
                _trainingDataStatus.value = statusText
                    ?: "サーバのラウンド進行を待っています（現在 round≤$base、最大 ${maxWaitMs / 60000} 分）。進まない場合は中央(8000)とエッジの CENTRAL_SERVER_URL を確認してください。"
            }
            while (true) {
                ensureActive()

                // タイムアウトチェック（サーバー集約が進まない場合の安全網）
                if (System.currentTimeMillis() > deadline) {
                    RealTimeLogger.w(TAG, "waitForServerRoundToAdvance: timed out after ${maxWaitMs}ms waiting for round > $base. Proceeding anyway.")
                    withContext(Dispatchers.Main) {
                        _trainingDataStatus.value =
                            "ラウンド待ちがタイムアウトしました（中央への集約送信失敗などで round が進んでいない可能性）。次のサイクルに進みます。"
                    }
                    return false
                }

                val meta = try {
                    networkClient.fetchMeta(forceRefresh = true)
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "waitForServerRoundToAdvance: fetchMeta failed: ${e.message}")
                    null
                }

                if (meta != null) {
                    if (meta.round > base) {
                        lastKnownRound = meta.round
                        try { persistLastKnownRound(meta.round) } catch (e: Exception) { RealTimeLogger.w(TAG, "persistLastKnownRound failed: ${e.message}") }
                        try { com.example.hfl_experiment.experiment.ExperimentContext.updateRound(getApplication(), meta.round) } catch (_: Exception) {}
                        awaitingModelAfterUpload = false
                        try { _serverRound.value = meta.round } catch (_: Exception) {}
                        try { viewModelScope.launch { _inferenceEvent.emit("${base} -> ${meta.round}") } } catch (_: Exception) {}
                        RealTimeLogger.i(TAG, "waitForServerRoundToAdvance: round advanced ${base} -> ${meta.round}")
                        return true
                    } else {
                        val remaining = (deadline - System.currentTimeMillis()) / 1000
                        RealTimeLogger.i(TAG, "waitForServerRoundToAdvance: round=${meta.round} not advanced yet (base=$base remaining=${remaining}s)")
                    }
                }

                val delayMs = pollIntervalMs.coerceAtLeast(1000L)
                try {
                    kotlinx.coroutines.delay(delayMs)
                } catch (ce: CancellationException) {
                    RealTimeLogger.i(TAG, "waitForServerRoundToAdvance: cancelled while delaying")
                    throw ce
                }
            }
        } catch (ce: CancellationException) {
            RealTimeLogger.i(TAG, "waitForServerRoundToAdvance: cancelled")
            return false
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "waitForServerRoundToAdvance failed: ${e.message}")
            return false
        }
    }

    // Copy logs from app filesDir/log to externalFilesDir/shared_logs for easier adb pulling (debug helper)
    suspend fun exportLogsForPull(): List<String> {
        return withContext(Dispatchers.IO) {
            try {
                val app = getApplication<Application>()
                val srcDir = File(app.filesDir, "log")
                val destBase = app.getExternalFilesDir("shared_logs") ?: app.filesDir
                if (!destBase.exists()) destBase.mkdirs()
                val copied = mutableListOf<String>()
                if (srcDir.exists() && srcDir.isDirectory) {
                    srcDir.listFiles()?.forEach { f ->
                        try {
                            val dest = File(destBase, f.name)
                            f.copyTo(dest, overwrite = true)
                            copied.add(dest.absolutePath)
                        } catch (e: Exception) {
                            RealTimeLogger.w(TAG, "exportLogsForPull: failed to copy ${f.name}: ${e.message}")
                        }
                    }
                }
                // also copy training_log.csv if present
                try {
                    val csv = File(app.filesDir, "log/training_log.csv")
                    if (csv.exists()) {
                        val destCsv = File(destBase, csv.name)
                        csv.copyTo(destCsv, overwrite = true)
                        copied.add(destCsv.absolutePath)
                    }
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "exportLogsForPull: failed to copy training_log.csv: ${e.message}")
                }
                RealTimeLogger.i(TAG, "exportLogsForPull: copied ${copied.size} files to ${destBase.absolutePath}")
                return@withContext copied
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "exportLogsForPull failed: ${e.message}")
                return@withContext emptyList<String>()
            }
        }
    }

    private fun readTerminalIndex(): Int? {
        return try {
            val sp = getApplication<Application>().getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
            if (sp.contains("terminal_index")) sp.getInt("terminal_index", 0) else null
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "readTerminalIndex failed: ${e.message}")
            null
        }
    }

    /**
     * record-connected-ap: 計測のたびに、実際に繋がっている AP と実測の TP・RTT をエッジ（→中央）に送る。
     * AP が取れないとき（null）は送らない。失敗しても計測は止めない（バックグラウンドで送り、警告だけ残す）。
     * HFL の学習・アップロードの経路には載せない。
     */
    private fun postApObservation(tpMbps: Double?, rttMs: Double?) {
        val ap = readConnectedAp().ap ?: return
        if (tpMbps == null && rttMs == null) return
        val tid = terminalId
        kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO).launch {
            try { networkClient.observeAp(tid, "AP$ap", tpMbps, rttMs) }
            catch (e: Exception) { RealTimeLogger.w(TAG, "postApObservation failed: ${e.message}") }
        }
    }

    // record-connected-ap: Greedy の判定で AP ごとに実測／模擬値のどちらを使ったかの回数（例 "AP1:measured" -> 7）。セッションごとに数え直す
    private val greedySourceCounts = java.util.concurrent.ConcurrentHashMap<String, Int>()

    // record-connected-ap: After の計測の開始時・終了時に実際に繋がっていた AP
    @Volatile private var apAtAfterStart: ConnectedAp = ConnectedAp(null, null)
    @Volatile private var apAtAfterEnd: ConnectedAp = ConnectedAp(null, null)
    @Volatile private var afterSettleMsUsed: Long = 0L

    // E8: 学習に使ったデータの部分（メトリクスに残す）
    @Volatile private var dataPartition: DataPartitionInfo? = null

    // rebuild-training-data-20261022: 学習に使った教師データの出所・SHA256・行数（メトリクスに残す）
    @Volatile private var trainingDataInfo: TrainingDataInfo? = null

    private fun recordTrainingData(source: String, sha256: String, rows: Int) {
        trainingDataInfo = TrainingDataInfo(source, sha256, rows)
        RealTimeLogger.i(TAG, "training data: source=$source rows=$rows sha256=$sha256")
    }

    private fun recordDataPartition(index: Int, count: Int, rows: Int, source: String) {
        dataPartition = DataPartitionInfo(index, count, rows, source)
        RealTimeLogger.i(TAG, "data partition: index=$index count=$count rows=$rows source=$source terminal_id=$terminalId")
        if (source == "default") RealTimeLogger.w(TAG, "data partition: terminal_index も端末 ID の番号も使えないため 0 番を使う (terminal_id=$terminalId)")
    }

    private fun readTerminalCount(): Int {
        return try {
            val sp = getApplication<Application>().getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
            sp.getInt("terminal_count", 4).coerceAtLeast(1)
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "readTerminalCount failed: ${e.message}")
            4
        }
    }
}

/**
 * サイクル初期値の選択（SP1 spec §3.1）。null を返すと LocalTrainer がダウンロード済みモデルを自動読込する（無ければランダム）。
 * 未使用の新しいダウンロード済みモデルが最優先（自動再開時の seed より優先, 衝突①）。無ければ seed/自モデルで続ける（衝突②）。
 */
/** SP3d 4.1: 新モデル受信時の表示（学習は自動で始めない）。 */
internal fun modelReceivedMessage(path: String?): String =
    "新しいモデルを受信しました（学習はボタン/adb から開始）: ${path ?: "(不明)"}"

internal fun chooseTrainingInit(downloadedSha: String?, lastUsedDownloadedSha: String?, provided: ModelParameters?): ModelParameters? =
    if (downloadedSha != null && downloadedSha != lastUsedDownloadedSha) null else provided

/** 学習に「使った」ダウンロード済みモデルの SHA。読込失敗も使用済みにし、壊れたモデルのせいで毎サイクルランダムにならないようにする。 */
internal fun usedDownloadedSha(initSource: String, downloadedSha: String?, previous: String?): String? =
    if (initSource == "DOWNLOADED" || initSource == "RANDOM_LOAD_FAILED") downloadedSha ?: previous else previous

/**
 * ベースラインモード切替の手順（SP-B）: 前セッションを確定 → 送達（未送信が無くなるか上限時間まで）→ 切替を記録。
 * 上限を超えても切り替える（未送信は端末に残り自動再送）。戻り値は記録した未送信件数。
 */
internal suspend fun runModeSwitchProcedure(
    finalize: suspend () -> Unit,
    deliver: suspend () -> Boolean,
    pendingCount: () -> Int,
    record: (undelivered: Int) -> Unit,
    timeoutMs: Long = 120_000L,
    retryIntervalMs: Long = 3_000L,
    now: () -> Long = System::currentTimeMillis,
    sleep: suspend (Long) -> Unit = { kotlinx.coroutines.delay(it) },
): Int {
    finalize()
    val deadline = now() + timeoutMs
    while (true) {
        if (deliver()) break
        if (now() >= deadline) break
        sleep(retryIntervalMs)
    }
    val undelivered = pendingCount()
    record(undelivered)
    return undelivered
}

/** SP3d: ベースラインの 1 サイクルあたりの計測回数（HFL の 1 サイクルのエポック数に相当）。 */
internal const val BASELINE_MEASUREMENTS_PER_CYCLE = 5

/** SP3d: ベースラインの計測回数 = サイクル数 × 5（既定 5 サイクル = 25 回、20 サイクル = 従来の 100 回）。 */
internal fun baselineMeasurementCount(numCycles: Int): Int = numCycles * BASELINE_MEASUREMENTS_PER_CYCLE

/** SP3d: 計測番号（1 始まり）→ 表示用の (サイクル, エポック)（いずれも 1 始まり）。 */
internal fun baselineCycleEpoch(measurement: Int): Pair<Int, Int> =
    ((measurement - 1) / BASELINE_MEASUREMENTS_PER_CYCLE + 1) to ((measurement - 1) % BASELINE_MEASUREMENTS_PER_CYCLE + 1)

/** 出力次元 = AP 数（先行研究準拠。ラベル = 接続すべき AP ID）。ラベルが AP 数を超えるデータは学習しない。 */
internal fun trainingOutputSize(maxLabel: Int, apCount: Int = com.example.hfl_experiment.AppConfig.AP_COUNT): Int {
    require(maxLabel < apCount) { "教師データのラベル $maxLabel が AP 数 $apCount 以上です（ラベルは AP ID 0..${apCount - 1} である必要があります）" }
    return apCount
}

