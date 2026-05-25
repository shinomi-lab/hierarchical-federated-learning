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
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
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
        private const val MAX_WEBSOCKET_EVENTS = 200
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
    // Current logical router id maintained by the client (used for uploads to simulate handover)
    // Default logical router id for this client (primary AP SSID)
    var currentRouterId: String = "HFL_A24"

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

    @Suppress("unused")
    fun recordWebSocketEvent(event: String) {
        try {
            val ts = System.currentTimeMillis()
            websocketEvents.add(ts to event)
            // Cap list size to prevent unbounded growth across rounds
            if (AppConfig.isMemoryOptEnabled(getApplication())) {
                while (websocketEvents.size > MAX_WEBSOCKET_EVENTS) {
                    websocketEvents.removeAt(0)
                }
            }
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
                // Also record a compact TrainingMetric into TrainingMetricsStore so TelemetrySender picks it up
                try {
                    try { TrainingMetricsStore.init(app) } catch (_: Exception) {}
                    val epochDurations = epochMetrics.map { it.durationMs }
                    val appIdx = _assignedAppIndex.value ?: 0
                    val metric = TrainingMetric(
                        terminalId = AppConfig.getTerminalId(app),
                        timestamp = ts,
                        totalLocalMs = totalMs,
                        epochDurations = epochDurations,
                        inputSize = modelInputSize.takeIf { it > 0 },
                        hiddenSize = modelHiddenSize.takeIf { it > 0 },
                        outputSize = modelOutputSize.takeIf { it > 0 },
                        dataId = null,
                        round = lastKnownRound ?: -1,
                        accuracy = avgValAcc,
                        loss = avgValLoss,
                        // 研究フィールド
                        runId = com.example.hfl_experiment.experiment.ExperimentContext.runId.ifEmpty { null },
                        experimentGroup = com.example.hfl_experiment.experiment.ExperimentContext.experimentGroup.ifEmpty { null },
                        cycleNumber = _currentCycle.value.takeIf { it > 0 },
                        epochCount = epochMetrics.size.takeIf { it > 0 },
                        valAccuracy = avgValAcc,
                        valLoss = avgValLoss,
                        dataSamplesCount = nSamples.takeIf { it > 0 },
                        batteryAtStart = batteryLevelBeforeTraining?.toDouble(),
                        batteryAtEnd = batteryLevelAfterTraining?.toDouble(),
                        memUsedMbAtStart = lastHeapBeforeBytes?.let { it / 1024.0 / 1024.0 },
                        memUsedMbAtEnd = lastHeapAfterBytes?.let { it / 1024.0 / 1024.0 },
                        networkTypeAtUpload = lastNetworkTransport,
                        uploadDurationMs = lastUploadPostMs,
                        satisfactionBefore = sessionSatisfactionBefore?.toDouble(),
                        satisfactionAfter = sessionSatisfactionAfter?.toDouble(),
                        appType = appConfigs?.getOrNull(appIdx)?.appType,
                        appIndex = appIdx,
                        modelVersion = com.example.hfl_experiment.experiment.ExperimentContext.modelVersion.ifEmpty { null },
                        tpMeasured = lastBandwidthMbps,
                        rttMeasured = lastMeasuredRttMs?.toDouble()
                    )
                    TrainingMetricsStore.record(metric)
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "persistTrainingMetrics: failed to record TrainingMetricsStore entry: ${e.message}")
                }
                RealTimeLogger.i(TAG, "persistTrainingMetrics: wrote csv=${csvFile.absolutePath} summary=${summaryFile.absolutePath}")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "persistTrainingMetrics failed: ${e.message}")
            }
        }
    }

    private var lastEpochDurationsMs: List<Long> = emptyList()
    private var lastLocalTrainingTotalMs: Long = 0L
    private var lastBandwidthMbps: Double? = null
    private var lastMeasuredRttMs: Float? = null

    private var currentRunJob: Job? = null
    private var uploadJob: Job? = null

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

    private val _totalCycles = MutableStateFlow(5)
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
     */
    suspend fun measureSatisfactionAfter(): Float? {
        RealTimeLogger.i(TAG, "measureSatisfactionAfter: 手動計測開始 (強制確定モード)")
        
        // 1. 現在の品質を計測 (label="after" で記録)
        val result = measureAndLogTerminalSatisfaction(null, label = "after")
        sessionSatisfactionAfter = result
        try { _satisfactionAfter.value = result } catch (_: Exception) {}

        // 2. 実験レコードを即座に更新保存
        if (currentSessionId.isNotEmpty()) {
            try { saveExperimentRecord() } catch (e: Exception) {
                RealTimeLogger.w(TAG, "measureSatisfactionAfter: saveExperimentRecord failed: ${e.message}")
            }
        }

        // 2b. TrainingMetricsStore の直近レコードも satisfaction_after を反映
        if (result != null) {
            try {
                TrainingMetricsStore.init(getApplication())
                TrainingMetricsStore.updateLastSatisfactionAfter(result.toDouble())
            } catch (_: Exception) {}
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

        // 5. satisfaction_after を含む training metrics をエッジサーバーへ送信
        kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO).launch {
            try {
                val app = getApplication<Application>()
                val baseUrl = AppConfig.getEdgeBaseUrl(app)
                val token = AppConfig.getServerAuthToken(app)
                val client = com.example.hfl_experiment.network.NetworkClient(context = app, baseUrl = baseUrl, authToken = token)
                val appIdx = _assignedAppIndex.value ?: 0
                val avgValAcc = epochMetrics.mapNotNull { it.valAcc }.let { if (it.isNotEmpty()) it.average() else null }
                val avgValLoss = epochMetrics.mapNotNull { it.valLoss }.let { if (it.isNotEmpty()) it.average() else null }
                val ok = client.sendTrainingMetric(
                    edgeId = AppConfig.getTerminalId(app),
                    round = lastKnownRound ?: 0,
                    accuracy = avgValAcc ?: -1.0,
                    loss = avgValLoss ?: -1.0,
                    appType = appConfigs?.getOrNull(appIdx)?.appType,
                    appIndex = appIdx,
                    satisfactionBefore = sessionSatisfactionBefore?.toDouble(),
                    satisfactionAfter = result?.toDouble(),
                    tpMeasured = lastBandwidthMbps,
                    rttMeasured = lastMeasuredRttMs?.toDouble()
                )
                RealTimeLogger.i(TAG, "measureSatisfactionAfter: training metrics 送信結果=$ok (satisfaction_after=$result)")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "measureSatisfactionAfter: training metrics 送信失敗: ${e.message}")
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

        val job = viewModelScope.launch {
            val myJob = coroutineContext[Job]
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
                                            val dto = try { networkClient.fetchSendToDevice() } catch (e: Exception) { RealTimeLogger.w(TAG, "Immediate fetchSendToDevice after meta change failed: ${e.message}"); null }

                                            if (dto != null) {
                                                val modelLink = dto.links?.model ?: dto.paths?.model_rel ?: dto.model?.path
                                                if (!modelLink.isNullOrBlank()) {
                                                    try {
                                                        val dlClient = okhttp3.OkHttpClient.Builder()
                                                            .connectTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
                                                            .readTimeout(120, java.util.concurrent.TimeUnit.SECONDS)
                                                            .writeTimeout(120, java.util.concurrent.TimeUnit.SECONDS)
                                                            .build()

                                                        val appCtx = getApplication<Application>()
                                                        if (modelLink.startsWith("http://") || modelLink.startsWith("https://")) {
                                                            RealTimeLogger.i(TAG, "Meta-detected immediate download: absolute model link found, triggering downloadFromUrl")
                                                            ModelUpdateUtil.downloadAndApplyModelFromUrlWithLock(
                                                                appCtx,
                                                                modelLink,
                                                                dlClient,
                                                                AppConfig.getServerAuthToken(appCtx),
                                                                null
                                                            )
                                                        } else {
                                                            val downloadBase = try {
                                                                val uri = java.net.URI(edgeBaseUrl)
                                                                val scheme = if (uri.scheme.equals("https", ignoreCase = true)) "https" else "http"
                                                                val portPart = if (uri.port == -1) "" else ":${uri.port}"
                                                                "${scheme}://${uri.host}${portPart}/download"
                                                            } catch (e: Exception) {
                                                                RealTimeLogger.w(TAG, "Failed to construct downloadBase from edgeBaseUrl: ${e.message}")
                                                                edgeBaseUrl.trimEnd('/') + "/download"
                                                            }
                                                            RealTimeLogger.i(TAG, "Meta-detected immediate download: relative model path found, triggering downloadWithLock (rel=$modelLink)")
                                                            ModelUpdateUtil.downloadAndApplyModelWithLock(appCtx, downloadBase, dlClient, AppConfig.getServerAuthToken(appCtx), modelLink, null)
                                                        }
                                                    } catch (e: Exception) {
                                                        RealTimeLogger.w(TAG, "Meta-detected immediate model download/apply attempt failed: ${e.message}")
                                                    }
                                                } else {
                                                    RealTimeLogger.i(TAG, "Meta-detected: fetchSendToDevice returned no model link; will rely on broadcast/polling")
                                                }
                                            }
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
                // Only release the flag if this is still the active polling job.
                // Prevents a cancelled old job from stealing the flag from a new one.
                if (metaPollingJob === myJob) {
                    metaPollingInFlight.set(false)
                }
                RealTimeLogger.i(TAG, "Meta polling stopped (myJob=${myJob}, currentJob=${metaPollingJob})")
            }
        }
        metaPollingJob = job
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

    fun fetchAndPrepareTrainingData(selectedApps: List<Int>? = null) {
         _trainingDataStatus.value = "学習データを準備中..."
         viewModelScope.launch {
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

                    // --- Prefer bundled assets/training_data.csv if present ---
                    try {
                        val am = getApplication<Application>().assets
                        var assetLoaded: List<DataSample>? = null
                        var assetPresent = false
                        try {
                            am.open("training_data.csv").use { ins ->
                                assetPresent = true
                                val tmp = java.io.File(getApplication<Application>().cacheDir, "training_data_from_assets.csv")
                                tmp.outputStream().use { outs -> ins.copyTo(outs) }
                                // count lines in the copied tmp file for clear diagnostics
                                val lineCount = try { tmp.useLines { it.count() } } catch (_: Exception) { -1 }
                                RealTimeLogger.i(TAG, "fetchAndPrepareTrainingData: asset training_data.csv copied to tmp, lines=${lineCount}")
                                assetLoaded = try { com.example.hfl_experiment.training.data.CsvLoader.loadFlexible(tmp, 2, appNumMax) } catch (e: Exception) { null }
                                try { if (tmp.exists()) tmp.delete() } catch (_: Exception) {}
                            }
                        } catch (_: Exception) { /* asset not present */ }

                        // If asset file exists but parser returned empty/null, raise explicit error so developer notices
                        if (assetPresent && assetLoaded.isNullOrEmpty()) {
                            _trainingDataStatus.value = "学習データ (assets) が存在しますがパースに失敗しました。アセットを確認してください。"
                            _errorMessage.value = "assets/training_data.csv が存在するがパースに失敗しました。CSVの形式を確認してください。"
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
                            val myIdx = readTerminalIndex() ?: 0
                            // chunkSize: ceil(normalized.size / tCount)
                            val chunkSize = (normalized.size + tCount - 1) / tCount
                            val parts = normalized.chunked(chunkSize)
                            val slice = if (myIdx in 0 until parts.size) parts[myIdx] else parts.getOrNull(0) ?: normalized

                            // assign to fullDataset the slice for this terminal
                            fullDataset = slice
                             modelInputSize = tpRttFeatures + appNumMax
                             modelOutputSize = appNumMax
                             modelInputSize = 2 + actualAppNum
                             modelHiddenSize = 32
                             modelOutputSize = actualAppNum
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
                                                val myIdx = readTerminalIndex() ?: 0
                                                // chunkSize: ceil(normalized.size / tCount)
                                                val chunkSize = (normalized.size + tCount - 1) / tCount
                                                val parts = normalized.chunked(chunkSize)
                                                val slice = if (myIdx in 0 until parts.size) parts[myIdx] else parts.getOrNull(0) ?: normalized

                                                fullDataset = slice
                             modelInputSize = tpRttFeatures + appNumMax
                             modelOutputSize = appNumMax
                                                 modelInputSize = tpRttFeatures + actualAppNum
                                                 modelHiddenSize = 32
                                                 modelOutputSize = actualAppNum
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
                // Release previous trainer to avoid two instances coexisting in memory
                if (AppConfig.isMemoryOptEnabled(getApplication())) localTrainer = null

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
                    logger = { message -> RealTimeLogger.d("LocalTrainer", message) },
                    appFilesDir = getApplication<Application>().filesDir,
                    memoryOptEnabled = AppConfig.isMemoryOptEnabled(getApplication())
                )
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

                withContext(Dispatchers.Main) {
                    _trainingFinished.value = true
                    _trainingProgress.value = 1.0f
                }

                try {
                    try { ServerStyleLogger.append("INFO", "TrainingViewModel", "startTraining: training started for epochs=${epochs}") } catch (_: Exception) {}
                     RealTimeLogger.i(TAG, "startTraining: about to call uploadUpdateSuspend()")
                    withContext(Dispatchers.IO) {
                        val ok = uploadUpdateSuspend()
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
                // Release previous trainer to avoid two instances coexisting in memory
                if (AppConfig.isMemoryOptEnabled(getApplication())) localTrainer = null

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
                    logger = { message -> RealTimeLogger.d("LocalTrainer", message) },
                    appFilesDir = getApplication<Application>().filesDir,
                    memoryOptEnabled = AppConfig.isMemoryOptEnabled(getApplication())
                )
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

                // persist metrics to external files for later retrieval (best-effort)
                try {
                    persistTrainingMetrics(fullDataset.size)
                } catch (e: Exception) {
                    RealTimeLogger.w(TAG, "persistTrainingMetrics invocation failed: ${e.message}")
                }

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
                        withTimeout(UPLOAD_TIMEOUT_MS) {
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
                                put("network_type_at_upload", getNetworkTransport())
                                put("battery_at_start", batteryLevelBeforeTraining ?: JSONObject.NULL)
                                put("battery_at_end", batteryLevelAfterTraining ?: JSONObject.NULL)
                                put("model_version", com.example.hfl_experiment.experiment.ExperimentContext.modelVersion)
                                put("tp_measured_mbps", lastBandwidthMbps ?: JSONObject.NULL)
                                put("rtt_measured_ms", lastMeasuredRttMs ?: JSONObject.NULL)
                                // Accuracy / Loss from latest training cycle
                                val _avgValAcc = epochMetrics.mapNotNull { it.valAcc }.let { if (it.isNotEmpty()) it.average() else null }
                                val _avgValLoss = epochMetrics.mapNotNull { it.valLoss }.let { if (it.isNotEmpty()) it.average() else null }
                                put("accuracy", _avgValAcc ?: JSONObject.NULL)
                                put("loss", _avgValLoss ?: JSONObject.NULL)
                            }.toString()

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
        uploadJob = viewModelScope.launch {
            try {
                val ok = uploadUpdateSuspend()
                RealTimeLogger.i(TAG, "uploadUpdate: uploadUpdateSuspend returned ok=$ok")
                if (!ok) {
                    RealTimeLogger.w(TAG, "uploadUpdate: upload failed")
                }
            } catch (e: Exception) {
                RealTimeLogger.e(TAG, "uploadUpdate: exception while uploading", e)
            } finally {
                uploadJob = null
                RealTimeLogger.i(TAG, "uploadUpdate: upload job cleared")
            }
        }
    }

    @Suppress("UNUSED_PARAMETER")
    fun cancelUpload() {
        try {
            uploadJob?.cancel()
            uploadJob = null
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
            try { uploadJob?.cancel() } catch (e: Exception) { RealTimeLogger.w(TAG, "cancel uploadJob failed: ${e.message}") }
            uploadJob = null
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
    /**
     * 完全リセット: サーバ接続先の再設定画面に戻る（従来動作）。
     * 端末ID・エッジURL等の初期設定もクリアする。
     */
    fun fullReset() {
        try {
            RealTimeLogger.i(TAG, "fullReset: initiating full reset")
            softReset()
            // 追加: サーバ接続の初回確認状態をリセット（次回起動時に再確認画面が表示される）
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
            // アプリ種別の割り当てをクリア（再設定画面で再選択可能にする）
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

    /**
     * ソフトリセット: 学習状態のみリセットし、接続先・端末ID・アプリ種別は保持する。
     * リセット後に自動でメタポーリングを再開し、次のラウンドを取得できる状態にする。
     * → タスクキルせずに次の試行を始めたい場合に使う。
     */
    fun softReset() {
        try {
            RealTimeLogger.i(TAG, "softReset: initiating soft reset")
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
                RealTimeLogger.w(TAG, "softReset: ExperimentContext clear failed: ${e.message}")
            }
            // 4. WebSocket側のラウンド記録もクリ���
            try {
                val ctx = getApplication<Application>()
                ctx.getSharedPreferences("hfl_prefs", Context.MODE_PRIVATE)
                    .edit()
                    .remove("last_applied_round")
                    .apply()
                RealTimeLogger.i(TAG, "softReset: last_applied_round cleared")
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "softReset: last_applied_round clear failed: ${e.message}")
            }
            // 5. StateFlow を初期値にリセット
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
            _currentCycle.value = 0
            _currentEpoch.value = 0
            // 6. 内部変数をクリア
            lastKnownRound = null
            awaitingModelAfterUpload = false
            fullDataset = emptyList()
            localTrainer = null
            sessionSatisfactionBefore = null
            sessionSatisfactionAfter = null
            currentSessionId = ""
            recentMetaRequests.clear()
            metaBackoffMs = 0L
            RealTimeLogger.i(TAG, "softReset: state cleared, restarting meta polling")
            // 7. メタポーリングを再開 → 自動で最新ラウンドを取得し学習再開可能にする
            startMetaPolling()
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "softReset failed: ${e.message}")
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
            _trainingDataStatus.value = "新しいモデルが適用されました: ${path ?: "(不明)"}"
            _trainingFinished.value = false
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

            try {
                if (awaitingModelAfterUpload && lastUploadedModelParameters != null && !_isTrainingRunning.value) {
                    RealTimeLogger.i(TAG, "notifyExternalModelUpdate: awaitingModelAfterUpload true and params present -> auto-starting training")
                    awaitingModelAfterUpload = false
                    val seed = lastUploadedModelParameters
                    val inputS = if (modelInputSize > 0) modelInputSize else (seed?.weights1?.firstOrNull()?.size ?: 0)
                    val hiddenS = if (modelHiddenSize > 0) modelHiddenSize else (seed?.weights2?.size ?: 0)
                    val outputS = if (modelOutputSize > 0) modelOutputSize else (seed?.weights3?.size ?: 0)
                    viewModelScope.launch {
                        try { runFiveCycles(inputS, hiddenS, outputS, epochs = 5, autoUpload = false, initialParams = seed) } catch (e: Exception) { RealTimeLogger.w(TAG, "Auto-start from notifyExternalModelUpdate failed: ${e.message}") }
                    }
                }
            } catch (e: Exception) {
                RealTimeLogger.w(TAG, "notifyExternalModelUpdate auto-start logic failed: ${e.message}")
            }

            try {
                val cfg = com.example.hfl_experiment.experiment.Config.loadConfigFromAssets(getApplication())
                if (decisionEngine == null) {
                    decisionEngine = com.example.hfl_experiment.experiment.DecisionEngine(getApplication(), cfg)
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
        // Release DecisionEngine (holds Context reference)
        try { decisionEngine = null } catch (_: Throwable) {}
        // Release LocalTrainer buffers
        try { localTrainer = null } catch (_: Throwable) {}
        // Shutdown NetworkClient's OkHttp resources
        try {
            networkClient.shutdown()
        } catch (_: Throwable) {}
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
                jo.put("round", lastKnownRound ?: JSONObject.NULL)
                jo.put("virtual_router_id", currentRouterId)

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

        // ExperimentContext に run_id を発行し、ログ・HTTP ヘッダ・フォームに自動反映させる
        currentSessionId = com.example.hfl_experiment.experiment.ExperimentContext
            .startNewRun(getApplication())
        sessionSatisfactionBefore = null
        sessionSatisfactionAfter = null
        try { _satisfactionBefore.value = null } catch (_: Exception) {}
        try { _satisfactionAfter.value = null } catch (_: Exception) {}

        // Initialize device metrics collector for this session
        try {
            com.example.hfl_experiment.telemetry.DeviceMetricsCollector.init(getApplication())
            com.example.hfl_experiment.telemetry.DeviceMetricsCollector.resetForSession(5 * (epochs))
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

        try {
            val cycles = 5
            _totalCycles.value = cycles
            _epochsPerCycle.value = epochs
            for (cycle in 1..cycles) {
                var traceCycleCookie = 0
                try {
                    // publish current cycle and reset epoch indicator
                    _currentCycle.value = cycle
                    _currentEpoch.value = 0
                    withContext(Dispatchers.Main) { _trainingDataStatus.value = "学習サイクル ${cycle}/$cycles 実行中..." }
                    traceCycleCookie = com.example.hfl_experiment.telemetry.HflTracer.beginAsync("HFL_cycle_$cycle")

                    // Use initialParams only for the first cycle (if provided)
                    val initParamsForThisCycle = if (cycle == 1) initialParams else null

                    // RealTimeLogger training start for first cycle
                    if (cycle == 1) {
                        try { com.example.hfl_experiment.util.logging.RealTimeLogger.trainingStart(epochs) } catch (_: Exception) {}
                    }

                    val ok = performLocalTrainingSuspend(
                        inputSize = inputSize,
                        hiddenSize = hiddenSize,
                        outputSize = outputSize,
                        epochs = epochs,
                        onProgress = { pf ->
                            // pf: 0..1 progress within the current cycle (epoch-level)
                            try {
                                val globalProgress = ((cycle - 1).toFloat() + pf) / cycles.toFloat()
                                // update UI on Main
                                 try { viewModelScope.launch(Dispatchers.Main) { _trainingProgress.value = globalProgress } } catch (_: Exception) {}
                                // update StateFlow directly to avoid spawning a coroutine for every small progress update +                                _trainingProgress.value = globalProgress
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
                        RealTimeLogger.w(TAG, "runFiveCycles: local training failed at cycle=$cycle")
                        withContext(Dispatchers.Main) { _errorMessage.value = "ローカル学習が失敗しました（cycle=$cycle）" }
                        try { com.example.hfl_experiment.util.logging.RealTimeLogger.anomaly("local_training_failed", "cycle=$cycle") } catch (_: Exception) {}
                        return false
                    }

                    // after each successful cycle, persist intermediate metrics (best-effort) and send telemetry
                    try {
                        persistTrainingMetrics(fullDataset.size)
                    } catch (e: Exception) { RealTimeLogger.w(TAG, "runFiveCycles: persistTrainingMetrics failed: ${e.message}") }

                    // update lastUploadedModelParameters candidate so notifyExternalModelUpdate auto-start logic can reuse if needed
                    try { lastUploadedModelParameters = localTrainer?.getModelParameters() } catch (e: Exception) { RealTimeLogger.w(TAG, "runFiveCycles: getModelParameters failed: ${e.message}") }

                    // Perform upload and telemetry after every cycle when autoUpload==true
                    if (autoUpload) {
                        try {
                            withContext(Dispatchers.IO) {
                                try { com.example.hfl_experiment.util.logging.RealTimeLogger.uploadStart(AppConfig.getEdgeBaseUrl(getApplication())) } catch (_: Exception) {}
                                val uploadOk = try {
                                    val res = uploadUpdateSuspend()
                                    RealTimeLogger.i(TAG, "runFiveCycles: uploadUpdateSuspend returned $res for cycle=$cycle")
                                    res
                                } catch (ce: CancellationException) {
                                    RealTimeLogger.i(TAG, "runFiveCycles: upload cancelled during cycle=$cycle")
                                    throw ce
                                } catch (e: Exception) {
                                    RealTimeLogger.e(TAG, "runFiveCycles: uploadUpdateSuspend threw for cycle=$cycle: ${e.message}", e)
                                    false
                                }
                                try { com.example.hfl_experiment.util.logging.RealTimeLogger.uploadEnd(AppConfig.getEdgeBaseUrl(getApplication()), uploadOk, null) } catch (_: Exception) {}

                                // send telemetry once after this upload attempt (best-effort)
                                try {
                                    // Force a fresh satisfaction measurement before sending telemetry
                                    try {
                                        RealTimeLogger.i(TAG, "runFiveCycles: forcing satisfaction measurement before telemetry send (cycle=$cycle)")
                                        // run measurement; label as "cycle_N" so it does not overwrite before/after records
                                        try { measureAndLogTerminalSatisfaction(null, label = "cycle_${cycle}") } catch (e: Exception) { RealTimeLogger.w(TAG, "force satisfaction measurement failed: ${e.message}") }
                                        // small buffer to ensure file write propagation
                                        try { kotlinx.coroutines.delay(500) } catch (_: Exception) {}
                                    } catch (e: Exception) {
                                        RealTimeLogger.w(TAG, "runFiveCycles: measurement step failed: ${e.message}")
                                    }

                                    val sent = TelemetrySender.send(getApplication())
                                    RealTimeLogger.i(TAG, "runFiveCycles: TelemetrySender.send returned=$sent for cycle=$cycle")
                                 } catch (e: Exception) {
                                     RealTimeLogger.w(TAG, "runFiveCycles: Telemetry send failed for cycle=$cycle: ${e.message}")
                                 }

                                if (!uploadOk) {
                                    withContext(Dispatchers.Main) { _errorMessage.value = "アップロードに失敗しました（cycle=$cycle）" }
                                    throw Exception("upload failed at cycle=$cycle")
                                }

                                // After a successful upload, wait until the server advertises a larger round number
                                val prevRound = uploadedRoundAfterUpload ?: lastKnownRound
                                try {
                                    val waited = waitForServerRoundToAdvance(prevRound)
                                    if (!waited) {
                                        RealTimeLogger.w(TAG, "runFiveCycles: waitForServerRoundToAdvance returned false or was cancelled (cycle=$cycle)")
                                    } else {
                                        RealTimeLogger.i(TAG, "runFiveCycles: server round advanced — continuing to next cycle (cycle=$cycle)")
                                    }
                                } catch (ce: CancellationException) {
                                    RealTimeLogger.i(TAG, "runFiveCycles: wait cancelled while waiting for server round (cycle=$cycle)")
                                    throw ce
                                } catch (e: Exception) {
                                    RealTimeLogger.w(TAG, "runFiveCycles: waitForServerRoundToAdvance failed: ${e.message}")
                                }
                            }
                        } catch (ce: CancellationException) {
                            RealTimeLogger.i(TAG, "runFiveCycles: cancelled during upload/wait at cycle=$cycle")
                            throw ce
                        } catch (e: Exception) {
                            RealTimeLogger.w(TAG, "runFiveCycles: aborting due to upload/wait failure at cycle=$cycle: ${e.message}")
                            return false
                        }
                    }

                     withContext(Dispatchers.Main) {
                         _trainingProgress.value = (cycle.toFloat() / cycles.toFloat())
                     }
                    com.example.hfl_experiment.telemetry.HflTracer.endAsync("HFL_cycle_$cycle", traceCycleCookie)

                 } catch (e: CancellationException) {
                     com.example.hfl_experiment.telemetry.HflTracer.endAsync("HFL_cycle_$cycle", traceCycleCookie)
                     RealTimeLogger.i(TAG, "runFiveCycles cancelled at cycle=$cycle")
                     throw e
                 } catch (e: Exception) {
                     com.example.hfl_experiment.telemetry.HflTracer.endAsync("HFL_cycle_$cycle", traceCycleCookie)
                     RealTimeLogger.e(TAG, "runFiveCycles: exception during cycle=$cycle", e)
                     withContext(Dispatchers.Main) { _errorMessage.value = "学習中に例外が発生しました: ${e.message}" }
                     return false
                 }
             }

             withContext(Dispatchers.Main) { _trainingProgress.value = 1.0f; _trainingFinished.value = true; _trainingDataStatus.value = "5サイクル学習完了 — AP切り替え後に「満足度(After)を計測」を押してください" }

             // Save final experiment record (satisfaction_after is null until user presses the measurement button)
             try { saveExperimentRecord() } catch (e: Exception) {
                 RealTimeLogger.w(TAG, "runFiveCycles: final saveExperimentRecord failed: ${e.message}")
             }

             // If autoUpload is false, we might still want to allow caller to trigger upload externally — no-op here
             com.example.hfl_experiment.telemetry.HflTracer.endAsync("HFL_runFiveCycles", traceSessionCookie)
             // Collect UsageStats snapshot (non-blocking, for interference analysis)
             com.example.hfl_experiment.telemetry.HflTracer.collectUsageStatsAsync(getApplication())
             // Persist device metrics for accuracy degradation analysis
             try {
                 com.example.hfl_experiment.telemetry.DeviceMetricsCollector.persist(
                     sessionId = currentSessionId, round = lastKnownRound ?: 0
                 )
             } catch (_: Exception) {}
             return true
         } finally {
             try { currentRunJob = null } catch (_: Exception) {}
             withContext(Dispatchers.Main) { _isTrainingRunning.value = false }
         }
     }

    // Poll /api/v1/meta until server round > previousRound. Default polling interval is 5000ms.
    private suspend fun waitForServerRoundToAdvance(
        previousRound: Int?,
        pollIntervalMs: Long = 5000L,
        maxWaitMs: Long = 10 * 60 * 1000L  // デフォルト最大10分で待機を打ち切る
    ): Boolean {
        val base = previousRound ?: lastKnownRound ?: 0
        val deadline = System.currentTimeMillis() + maxWaitMs
        RealTimeLogger.i(TAG, "waitForServerRoundToAdvance: waiting for round > $base (poll=${pollIntervalMs}ms maxWait=${maxWaitMs}ms)")
        try {
            withContext(Dispatchers.Main) {
                _trainingDataStatus.value =
                    "サーバのラウンド進行を待っています（現在 round≤$base、最大 ${maxWaitMs / 60000} 分）。進まない場合は中央(8000)とエッジの CENTRAL_SERVER_URL を確認してください。"
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
