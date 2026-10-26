package com.example.hfl_experiment

import android.content.Context
import android.net.wifi.WifiManager
import com.example.hfl_experiment.experiment.ExperimentContext
import com.example.hfl_experiment.util.logging.RealTimeLogger
import java.net.URI

/**
 * アプリ内の簡易設定。テスト用に定義しています。
 * 将来的に BuildConfig or secure storage に差し替えてください。
 */
object AppConfig {
    // SharedPreferences 名
    private const val PREFS_ENV = "env_prefs"
    private const val PREF_EDGE_ENDPOINT_CONFIGURED = "edge_endpoint_configured"
    /**
     * true のとき [getEdgeBaseUrl] は接続 Wi-Fi の SSID に応じてポートを 8001/8002 に書き換える。
     * 設定画面や初回セットアップでホスト+ポートを明示保存した場合は false にし、UI のポート（例: 8002）を尊重する。
     */
    private const val PREF_EDGE_SSID_PORT_REWRITE = "edge_ssid_port_rewrite"
    /** 同一 APK 運用: 初回に端末 ID をユーザーが確定したか（既存インストールは migrate で true 化） */
    private const val PREF_TERMINAL_IDENTITY_CONFIGURED = "terminal_identity_configured"

    // Fallback literals (mirror values currently set in app/build.gradle.kts)
    private const val DEFAULT_EDGE_BASE_URL = "http://192.168.11.6:8001"
    private const val DEFAULT_SERVER_AUTH_TOKEN = "" // Set via SettingsActivity

    // Helper: try to read BuildConfig.<field> via reflection to avoid compile-time dependency
    private fun tryGetBuildConfigFieldString(context: Context, fieldName: String): String? {
        return try {
            val pkg = context.packageName
            val cls = Class.forName("$pkg.BuildConfig")
            val f = cls.getDeclaredField(fieldName)
            f.isAccessible = true
            f.get(null) as? String
        } catch (_: Exception) {
            null
        }
    }

    /** 現在接続 Wi-Fi の SSID（引用符除去）。取得できなければ null。 */
    private fun readCurrentWifiSsid(context: Context): String? {
        return try {
            val wm = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val raw = wm.connectionInfo?.ssid
            if (raw.isNullOrBlank()) null else raw.trim('"')
        } catch (e: Exception) {
            RealTimeLogger.w("AppConfig", "readCurrentWifiSsid: ${e.message}")
            null
        }
    }

    /**
     * 研究室構成に合わせ、接続 AP（SSID）に応じてエッジ HTTP(S) ポートを 8001 / 8002 に切り替える。
     * [getPrimarySsid] → 8001、[getSecondarySsid] → 8002、それ以外・SSID 不明は 8001。
     * `ws://` / `wss://` およびパース不能な URL は変更しない。
     */
    private fun rewriteEdgePortForCurrentWifi(context: Context, baseUrl: String): String {
        val u = try {
            URI(baseUrl)
        } catch (_: Exception) {
            return baseUrl
        }
        val scheme = u.scheme?.lowercase() ?: return baseUrl
        if (scheme == "ws" || scheme == "wss") return baseUrl
        if (scheme != "http" && scheme != "https") return baseUrl
        val host = u.host ?: return baseUrl
        val ssid = readCurrentWifiSsid(context)
        val chosenPort = when (ssid) {
            getPrimarySsid(context) -> 8001
            getSecondarySsid(context) -> 8002
            else -> 8001
        }
        return "$scheme://$host:$chosenPort"
    }

    // BuildConfig の値をフォールバックとして使い、実行時に SettingsActivity 等で上書き可能にする
    // これにより管理ポイントを一箇所に集約できます。
    fun getEdgeBaseUrl(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        val edgeHost = prefs.getString("edge_host", null)?.trim()
        val edgePort = prefs.getString("edge_port", null)?.trim()

        val raw = if (!edgeHost.isNullOrBlank()) {
            if (edgeHost.startsWith("http://", ignoreCase = true)
                || edgeHost.startsWith("https://", ignoreCase = true)
                || edgeHost.startsWith("ws://", ignoreCase = true)
                || edgeHost.startsWith("wss://", ignoreCase = true)
            ) {
                edgeHost
            } else {
                val portPart = if (!edgePort.isNullOrBlank()) ":${edgePort}" else ""
                "http://$edgeHost$portPart"
            }
        } else {
            tryGetBuildConfigFieldString(context, "EDGE_BASE_URL") ?: DEFAULT_EDGE_BASE_URL
        }
        val ssidPortRewrite = prefs.getBoolean(PREF_EDGE_SSID_PORT_REWRITE, true)
        return if (ssidPortRewrite) rewriteEdgePortForCurrentWifi(context, raw) else raw
    }

    /**
     * 既存インストール: `edge_host` + `edge_port`、または `http(s)://…:port` 形式でポートが埋まっている場合は
     * SSID によるポート上書きを無効化する（アップデート前から 8002 を指定していた端末向け）。
     */
    fun migrateEdgeSsidPortRewriteFlagIfNeeded(context: Context) {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        if (prefs.contains(PREF_EDGE_SSID_PORT_REWRITE)) return
        val eh = prefs.getString("edge_host", null)?.trim().orEmpty()
        if (eh.isBlank()) return
        val ep = prefs.getString("edge_port", null)?.trim()
        var disableRewrite = false
        if (!ep.isNullOrBlank()) disableRewrite = true
        else if (eh.startsWith("http://", ignoreCase = true) || eh.startsWith("https://", ignoreCase = true)) {
            try {
                val u = URI(eh)
                if (u.port != -1) disableRewrite = true
            } catch (_: Exception) { }
        }
        if (disableRewrite) {
            prefs.edit().putBoolean(PREF_EDGE_SSID_PORT_REWRITE, false).apply()
            RealTimeLogger.i(
                "AppConfig",
                "migrateEdgeSsidPortRewrite: disabled SSID port rewrite (existing explicit endpoint)"
            )
        }
    }

    /** 設定保存時に false、既定ビルドに戻すときはキー削除で true 相当 */
    fun setEdgeSsidPortRewriteEnabled(context: Context, enabled: Boolean) {
        val e = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).edit()
        if (enabled) e.remove(PREF_EDGE_SSID_PORT_REWRITE) else e.putBoolean(PREF_EDGE_SSID_PORT_REWRITE, false)
        e.apply()
    }

    /** BuildConfig または [DEFAULT_EDGE_BASE_URL] のみ。`edge_host` の SharedPreferences は見ない。 */
    fun getCompiledDefaultEdgeBaseUrl(context: Context): String =
        tryGetBuildConfigFieldString(context, "EDGE_BASE_URL") ?: DEFAULT_EDGE_BASE_URL

    /**
     * ユーザーがエッジ接続先を確認済みか。初回は false のため起動時に接続先確認画面を出す。
     * 既存インストールでは `edge_host` が入っていれば確認済みとみなす（移行用）。
     */
    fun isEdgeEndpointUserConfirmed(context: Context): Boolean {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        if (prefs.getBoolean(PREF_EDGE_ENDPOINT_CONFIGURED, false)) return true
        val eh = prefs.getString("edge_host", null)?.trim()
        if (!eh.isNullOrBlank()) return true
        return false
    }

    fun markEdgeEndpointUserConfirmed(context: Context) {
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(PREF_EDGE_ENDPOINT_CONFIGURED, true)
            .apply()
    }

    /**
     * 既存インストール: `terminal_id` が env_prefs に既に入っている場合は確認済みとみなす（初回端末 ID 画面を出さない）。
     */
    fun migrateTerminalIdentityConfiguredFlagIfNeeded(context: Context) {
        val p = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        if (p.getBoolean(PREF_TERMINAL_IDENTITY_CONFIGURED, false)) return
        val tid = p.getString("terminal_id", null)?.trim()
        if (!tid.isNullOrBlank()) {
            p.edit().putBoolean(PREF_TERMINAL_IDENTITY_CONFIGURED, true).apply()
        }
    }

    fun isTerminalIdentityConfigured(context: Context): Boolean =
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
            .getBoolean(PREF_TERMINAL_IDENTITY_CONFIGURED, false)

    fun markTerminalIdentityConfigured(context: Context) {
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(PREF_TERMINAL_IDENTITY_CONFIGURED, true)
            .apply()
    }

    /** 端末 ID を変更した直後に ExperimentContext 等へ反映する */
    fun clearTerminalIdMemoryCache() {
        cachedTerminalId = null
    }

    /**
     * 初回セットアップで確定した端末 ID と実験群を保存する。
     */
    fun applyTerminalIdentity(context: Context, terminalId: String, experimentGroup: String) {
        val tid = terminalId.trim()
        val grp = experimentGroup.trim()
        val env = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        env.edit()
            .putString("terminal_id", tid)
            .putBoolean(PREF_TERMINAL_IDENTITY_CONFIGURED, true)
            .apply()
        cachedTerminalId = tid
        markManualConfig(context)   // SP3d 4.2: 端末 ID 画面での保存は手動設定
        context.getSharedPreferences("experiment_context", Context.MODE_PRIVATE)
            .edit()
            .putString("experiment_group", grp)
            .apply()
        ExperimentContext.initFromAppConfig(context)
    }

    // サーバトークンも実行時に prefs で上書きできるようにする
    fun getServerAuthToken(context: Context): String? {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        val t = prefs.getString("server_auth_token", null)
        return if (!t.isNullOrBlank()) t else (tryGetBuildConfigFieldString(context, "SERVER_AUTH_TOKEN") ?: DEFAULT_SERVER_AUTH_TOKEN)
    }

    // Number of application categories (one-hot size / output classes). Change here to affect the whole app.
    const val APP_CAT_COUNT: Int = 4

    // AP 数（= AP 選択モデルの出力クラス数。先行研究準拠でラベルは AP ID）。アプリ数（入力の one-hot 長）とは別
    const val AP_COUNT: Int = 2

    // Baseline experiment mode: "none" = normal HFL, "fixed" = always AP0, "random" = random AP, "greedy" = best satisfaction,
    // "central" = 学習せず中央サーバへ問い合わせて推奨 AP を記録する対照群（centralized-mode。ベースラインではないが制御の仕組みを共有するため同じ設定項目）
    // 以前は const "fixed" 固定（このブランチの APK は学習しなかった）。設定画面 / adb (SET_CONFIG --es baseline_mode) で切替える。
    // "hfl_rec" = central と同じ流れで、最新のグローバルモデルの推奨 AP を記録し、その AP への切替を提案する（hfl-recommended-ap-mode）
    // "fixed_ap1" = 常に AP1（AP1 の定常の満足度を測る）。"random" はセッションの最初に 1 回だけ AP を決める（fix-audit-20261026）
    val BASELINE_MODES = listOf("none", "fixed", "random", "greedy", "central", "hfl_rec", "fixed_ap1")
    const val DEFAULT_BASELINE_MODE: String = "none"
    private const val KEY_BASELINE_MODE = "baseline_mode"

    private const val KEY_LAST_SESSION_BASELINE_MODE = "last_session_baseline_mode"

    // central モード（centralized-mode）の問い合わせ先。端末はエッジの URL しか知らないので別に持つ。未設定は空＝問い合わせない
    private const val KEY_CENTRAL_URL = "central_url"
    fun getCentralUrl(context: Context): String =
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).getString(KEY_CENTRAL_URL, null)?.trim()?.trimEnd('/').orEmpty()
    fun setCentralUrl(context: Context, url: String) {
        val v = url.trim().trimEnd('/')
        val e = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).edit()
        if (v.isEmpty()) e.remove(KEY_CENTRAL_URL) else e.putString(KEY_CENTRAL_URL, v)
        e.apply()
    }

    // SP-B: アプリ設定・サーバ指定（直近の /meta）・現在セッションのモードを分けて持つ
    private val appModeState = kotlinx.coroutines.flow.MutableStateFlow(DEFAULT_BASELINE_MODE)
    private val serverModeState = kotlinx.coroutines.flow.MutableStateFlow<String?>(null)
    private val baselineModeState = kotlinx.coroutines.flow.MutableStateFlow(DEFAULT_BASELINE_MODE)
    private val sourceState = kotlinx.coroutines.flow.MutableStateFlow("app")
    @Volatile private var sessionActive = false

    /** UI 表示用: 現在（セッション中はそのセッション）のモード。 */
    val baselineModeFlow: kotlinx.coroutines.flow.StateFlow<String> get() = baselineModeState
    /** UI 表示用: 現在のモードの出所（"server" / "app"）。 */
    val baselineModeSource: kotlinx.coroutines.flow.StateFlow<String> get() = sourceState
    /** UI 表示用: アプリ設定のモード。 */
    val appBaselineModeFlow: kotlinx.coroutines.flow.StateFlow<String> get() = appModeState
    /** UI 表示用: 直近のサーバ指定（null は指定なし）。 */
    val serverBaselineMode: kotlinx.coroutines.flow.StateFlow<String?> get() = serverModeState

    /** 現在セッションのモード。セッション中は変わらない（DecisionEngine 等が参照）。 */
    val BASELINE_MODE: String get() = baselineModeState.value

    /** アプリ設定のモード。 */
    val appBaselineMode: String get() = appModeState.value

    fun loadBaselineMode(context: Context) {
        val v = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).getString(KEY_BASELINE_MODE, null)
        appModeState.value = v?.takeIf { it in BASELINE_MODES } ?: DEFAULT_BASELINE_MODE
        if (!sessionActive) applyResolved()
    }

    fun setBaselineMode(context: Context, mode: String) {
        require(mode in BASELINE_MODES) { "unknown baseline_mode: $mode (allowed: $BASELINE_MODES)" }
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).edit().putString(KEY_BASELINE_MODE, mode).apply()
        appModeState.value = mode
        if (!sessionActive) applyResolved()
    }

    /** /meta で受け取ったサーバ指定を反映する（セッション中は「次セッションから」の予定になるだけ）。 */
    fun updateServerBaselineMode(mode: String?) {
        serverModeState.value = mode
        if (!sessionActive) applyResolved()
    }

    private fun applyResolved() {
        val (m, src) = resolveBaselineMode(serverModeState.value, appModeState.value)
        baselineModeState.value = m
        sourceState.value = src
    }

    /**
     * セッション開始時に呼ぶ: サーバ優先でモードを決めて固定し、前セッションのモードとの組を返す（変化の判定用）。
     * 前セッションのモードは永続化しているので、再起動をまたいでも比較できる。
     */
    fun beginSessionBaselineMode(context: Context): Pair<String, String> {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        val (m, src) = resolveBaselineMode(serverModeState.value, appModeState.value)
        val prev = prefs.getString(KEY_LAST_SESSION_BASELINE_MODE, null) ?: m
        baselineModeState.value = m
        sourceState.value = src
        sessionActive = true
        prefs.edit().putString(KEY_LAST_SESSION_BASELINE_MODE, m).apply()
        return prev to m
    }

    /** セッション終了時に呼ぶ: 以後の設定変更・サーバ指定を即時に表示へ反映する。 */
    fun endSessionBaselineMode() {
        sessionActive = false
        applyResolved()
    }

    /** 次セッションで変わる予定のモード（現在と同じなら null）。 */
    fun pendingBaselineMode(): String? =
        resolveBaselineMode(serverModeState.value, appModeState.value).first.takeIf { it != baselineModeState.value }

    // HFL synchronization mode:
    //   "sync"  = 既存の同期型（毎サイクル、サーバのラウンド進行を待つ）
    //   "async" = 閾値ベース非同期型（学習完了次第即時アップロード、新モデル無くても次サイクル継続）
    // SP3b R1: 以前はビルド時定数（async-hfl マージで "async"）。現在はエッジの /meta の hfl_mode を優先し、
    // 無いときはアプリ設定（設定画面 / adb SET_CONFIG --es hfl_mode、既定 sync）を使う。セッション開始時に固定する。
    val HFL_MODES = listOf("sync", "async")
    const val DEFAULT_HFL_MODE: String = "sync"
    private const val KEY_HFL_MODE = "hfl_mode"

    private val appHflModeState = kotlinx.coroutines.flow.MutableStateFlow(DEFAULT_HFL_MODE)
    private val serverHflModeState = kotlinx.coroutines.flow.MutableStateFlow<String?>(null)
    private val hflModeState = kotlinx.coroutines.flow.MutableStateFlow(DEFAULT_HFL_MODE)
    private val hflModeSourceState = kotlinx.coroutines.flow.MutableStateFlow("app")
    @Volatile private var hflSessionActive = false

    /** UI 表示用: 現在（セッション中はそのセッション）の HFL モード。 */
    val hflModeFlow: kotlinx.coroutines.flow.StateFlow<String> get() = hflModeState
    /** UI 表示用: 現在の HFL モードの出所（"server" / "app"）。 */
    val hflModeSource: kotlinx.coroutines.flow.StateFlow<String> get() = hflModeSourceState
    /** UI 表示用: アプリ設定の HFL モード。 */
    val appHflModeFlow: kotlinx.coroutines.flow.StateFlow<String> get() = appHflModeState
    /** UI 表示用: 直近の /meta の hfl_mode（null は未取得・旧サーバ）。 */
    val serverHflMode: kotlinx.coroutines.flow.StateFlow<String?> get() = serverHflModeState

    /** 現在セッションの HFL モード。セッション中は変わらない。 */
    val hflMode: String get() = hflModeState.value

    /** アプリ設定の HFL モード。 */
    val appHflMode: String get() = appHflModeState.value

    fun loadHflMode(context: Context) {
        val v = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).getString(KEY_HFL_MODE, null)
        appHflModeState.value = v?.takeIf { it in HFL_MODES } ?: DEFAULT_HFL_MODE
        if (!hflSessionActive) applyResolvedHflMode()
    }

    fun setHflMode(context: Context, mode: String) {
        require(mode in HFL_MODES) { "unknown hfl_mode: $mode (allowed: $HFL_MODES)" }
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).edit().putString(KEY_HFL_MODE, mode).apply()
        appHflModeState.value = mode
        if (!hflSessionActive) applyResolvedHflMode()
    }

    /** /meta で受け取った hfl_mode を反映する（セッション中は「次セッションから」の予定になるだけ）。 */
    fun updateServerHflMode(mode: String?) {
        serverHflModeState.value = mode
        if (!hflSessionActive) applyResolvedHflMode()
    }

    private fun applyResolvedHflMode() {
        val (m, src) = resolveHflMode(serverHflModeState.value, appHflModeState.value)
        hflModeState.value = m
        hflModeSourceState.value = src
    }

    /**
     * セッション開始時に呼ぶ: サーバ優先で HFL モードを決めて固定する。
     * 戻り値は (モード, 出所, 食い違い時のアプリ設定値)。3つ目はサーバ値を使い、かつアプリ設定と違うときだけ非 null。
     */
    fun beginSessionHflMode(): Triple<String, String, String?> {
        val app = appHflModeState.value
        val (m, src) = resolveHflMode(serverHflModeState.value, app)
        hflModeState.value = m
        hflModeSourceState.value = src
        hflSessionActive = true
        return Triple(m, src, app.takeIf { src == "server" && it != m })
    }

    /** セッション終了時に呼ぶ: 以後の設定変更・サーバ値を即時に表示へ反映する。 */
    fun endSessionHflMode() {
        hflSessionActive = false
        applyResolvedHflMode()
    }

    /** 次セッションで変わる予定の HFL モード（現在と同じなら null）。 */
    fun pendingHflMode(): String? =
        resolveHflMode(serverHflModeState.value, appHflModeState.value).first.takeIf { it != hflModeState.value }

    // SP3d: 1 セッションのサイクル数。エッジの /meta の num_cycles（中央の実験制御、set_mode.py --num-cycles）を優先し、
    // 無い・範囲外のときはアプリ設定（設定画面 / adb SET_CONFIG --ei num_cycles、既定 5）を使う。セッション開始時に固定する。
    const val NUM_CYCLES_MIN: Int = 1
    const val NUM_CYCLES_MAX: Int = 100
    const val DEFAULT_NUM_CYCLES: Int = 5
    private const val KEY_NUM_CYCLES = "num_cycles"

    private val appNumCyclesState = kotlinx.coroutines.flow.MutableStateFlow(DEFAULT_NUM_CYCLES)
    private val serverNumCyclesState = kotlinx.coroutines.flow.MutableStateFlow<Int?>(null)
    private val numCyclesState = kotlinx.coroutines.flow.MutableStateFlow(DEFAULT_NUM_CYCLES)
    private val numCyclesSourceState = kotlinx.coroutines.flow.MutableStateFlow("app")
    @Volatile private var numCyclesSessionActive = false

    /** UI 表示用: 現在（セッション中はそのセッション）のサイクル数。 */
    val numCyclesFlow: kotlinx.coroutines.flow.StateFlow<Int> get() = numCyclesState
    /** UI 表示用: 現在のサイクル数の出所（"server" / "app"）。 */
    val numCyclesSource: kotlinx.coroutines.flow.StateFlow<String> get() = numCyclesSourceState
    /** UI 表示用: アプリ設定のサイクル数。 */
    val appNumCyclesFlow: kotlinx.coroutines.flow.StateFlow<Int> get() = appNumCyclesState
    /** UI 表示用: 直近の /meta の num_cycles（null は指定なし）。 */
    val serverNumCycles: kotlinx.coroutines.flow.StateFlow<Int?> get() = serverNumCyclesState

    /** 現在セッションのサイクル数。セッション中は変わらない。 */
    val numCycles: Int get() = numCyclesState.value

    /** アプリ設定のサイクル数。 */
    val appNumCycles: Int get() = appNumCyclesState.value

    fun isValidNumCycles(n: Int?): Boolean = n != null && n in NUM_CYCLES_MIN..NUM_CYCLES_MAX

    fun loadNumCycles(context: Context) {
        val v = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).getInt(KEY_NUM_CYCLES, DEFAULT_NUM_CYCLES)
        appNumCyclesState.value = if (isValidNumCycles(v)) v else DEFAULT_NUM_CYCLES
        if (!numCyclesSessionActive) applyResolvedNumCycles()
    }

    /** アプリ設定のサイクル数を保存する。範囲外は IllegalArgumentException（保存しない）。 */
    fun setNumCycles(context: Context, n: Int) {
        require(isValidNumCycles(n)) { "num_cycles must be in $NUM_CYCLES_MIN..$NUM_CYCLES_MAX: $n" }
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).edit().putInt(KEY_NUM_CYCLES, n).apply()
        appNumCyclesState.value = n
        if (!numCyclesSessionActive) applyResolvedNumCycles()
    }

    /** /meta で受け取った num_cycles を反映する（セッション中は「次セッションから」の予定になるだけ）。 */
    fun updateServerNumCycles(n: Int?) {
        serverNumCyclesState.value = n
        if (!numCyclesSessionActive) applyResolvedNumCycles()
    }

    private fun applyResolvedNumCycles() {
        val (n, src) = resolveNumCycles(serverNumCyclesState.value, appNumCyclesState.value)
        numCyclesState.value = n
        numCyclesSourceState.value = src
    }

    /** セッション開始時に呼ぶ: サーバ優先でサイクル数を決めて固定する。戻り値は (サイクル数, 出所)。 */
    fun beginSessionNumCycles(): Pair<Int, String> {
        val r = resolveNumCycles(serverNumCyclesState.value, appNumCyclesState.value)
        numCyclesState.value = r.first
        numCyclesSourceState.value = r.second
        numCyclesSessionActive = true
        return r
    }

    /** セッション終了時に呼ぶ: 以後の設定変更・サーバ値を即時に表示へ反映する。 */
    fun endSessionNumCycles() {
        numCyclesSessionActive = false
        applyResolvedNumCycles()
    }

    /** 次セッションで変わる予定のサイクル数（現在と同じなら null）。 */
    fun pendingNumCycles(): Int? =
        resolveNumCycles(serverNumCyclesState.value, appNumCyclesState.value).first.takeIf { it != numCyclesState.value }

    // 非同期モード時の同期待ちタイムアウト（ms）。これより長く待たない（=待たずに次サイクルへ）

    const val ASYNC_ROUND_WAIT_TIMEOUT_MS: Long = 1000L

    /** セッション開始時（cycle1 の前）に最新の配布モデルを取りに行く上限（ms）。超えたら手元のモデルで始める（問題7）。 */
    const val SESSION_START_MODEL_FETCH_TIMEOUT_MS: Long = 15_000L

    /** 現在セッションの HFL モードが async か（セッション外では直近の解決結果）。 */
    fun isAsyncMode(): Boolean = hflMode == "async"

    // NOTE: The following router credentials and secrets are currently hardcoded for testing.
    // It's strongly recommended to remove these and use secure storage / settings.
    const val ROUTER_SSID_A: String = "HFL_A24"
    const val ROUTER_PASS_A: String = "" // set locally; never commit real credentials
    const val ROUTER_BSSID_A: String = "" // 任意
    const val ROUTER_IP_A: String = "" // 任意

    const val ROUTER_SSID_B: String = "HFL_B"
    const val ROUTER_PASS_B: String = ""
    const val ROUTER_BSSID_B: String = "" // 任意
    const val ROUTER_IP_B: String = "" // 任意

    // SSID getters: allow runtime override via SharedPreferences, fallback to constants
    fun getPrimarySsid(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        val v = prefs.getString("router_ssid_a", null)?.trim()
        return if (!v.isNullOrBlank()) v else ROUTER_SSID_A
    }

    // ap-detection-fallback: SSID が隠される端末で AP をサブネットから判定する。既定はラボの構成（A の LAN と、ルータ B の LAN 側）
    fun getSubnetA(context: Context): String {
        val v = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).getString("router_subnet_a", null)?.trim()
        return if (!v.isNullOrBlank()) v else com.example.hfl_experiment.training.DEFAULT_SUBNET_A
    }

    fun getSubnetB(context: Context): String {
        val v = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).getString("router_subnet_b", null)?.trim()
        return if (!v.isNullOrBlank()) v else com.example.hfl_experiment.training.DEFAULT_SUBNET_B
    }

    fun getSecondarySsid(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        val v = prefs.getString("router_ssid_b", null)?.trim()
        return if (!v.isNullOrBlank()) v else ROUTER_SSID_B
    }

    // WebSocket とダウンロードのパス（サーバ実装に合わせる）
    const val WS_PATH: String = "/ws/updates"
    const val DOWNLOAD_PATH: String = "/download"

    // --- New helpers to centralize URL construction ---
    // Returns download base like: http(s)://host:port/download
    fun getDownloadBaseUrl(context: Context): String {
        val base = getEdgeBaseUrl(context).trimEnd('/')
        return base + DOWNLOAD_PATH
    }

    // Returns websocket URL (ws:// or wss:// chosen based on edge base scheme) with optional token query
    fun getWebSocketUrl(context: Context, token: String?): String {
        val base = getEdgeBaseUrl(context)
        val wsScheme = if (base.startsWith("https")) "wss" else "ws"
        val uri = java.net.URI(base)
        val host = uri.host
        val port = if (uri.port != -1) ":${uri.port}" else ""
        val url = "$wsScheme://$host$port$WS_PATH"
        return if (token.isNullOrBlank()) url else "$url?token=$token"
    }

    // --- IP-based Auto Configuration ---
    // WiFi IPアドレスから端末IDとエッジURLを自動設定する。
    // 固定IP環境（実験ラボ）専用。起動のたびに同じ値を書くので冪等。
    // SP3d 4.2（R8）: 端末 ID・エッジ URL の設定の出所。手動設定（adb の terminal_id/edge_url、設定画面のエッジ Host/Port 変更、
    // エッジ接続先画面・端末 ID 画面の保存）があれば、IP からの自動設定で上書きしない。
    const val CONFIG_SOURCE_MANUAL = "manual"
    const val CONFIG_SOURCE_IP_AUTO = "ip_auto"
    private const val KEY_CONFIG_SOURCE = "config_source"

    private data class IpDeviceCfg(val terminalId: String, val edgeUrl: String)
    private val IP_DEVICE_MAPPING = mapOf(
        "192.168.11.11" to IpDeviceCfg("terminal-01", "http://192.168.11.6:8001"),
        "192.168.11.12" to IpDeviceCfg("terminal-02", "http://192.168.11.6:8001"),
        "192.168.11.13" to IpDeviceCfg("terminal-03", "http://192.168.11.6:8002"),
        "192.168.11.14" to IpDeviceCfg("terminal-04", "http://192.168.11.6:8002"),
    )

    /** 設定の出所（"manual" / "ip_auto" / null=未設定）。 */
    fun configSource(context: Context): String? =
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).getString(KEY_CONFIG_SOURCE, null)

    fun markManualConfig(context: Context) {
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).edit().putString(KEY_CONFIG_SOURCE, CONFIG_SOURCE_MANUAL).commit()
    }

    fun clearManualConfig(context: Context) {
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).edit().remove(KEY_CONFIG_SOURCE).commit()
    }

    /**
     * IP からの自動設定の本体（テスト用に IP を受け取る）。出所が manual なら何もせず false。対応表に無い IP も false。
     * それ以外は端末 ID・エッジ URL を書き込み、出所を ip_auto にして true。
     */
    fun applyIpAutoConfig(context: Context, ip: String?): Boolean {
        if (configSource(context) == CONFIG_SOURCE_MANUAL) {
            RealTimeLogger.i("AppConfig", "autoConfigureFromIp: manual config present, skipping (ip=$ip)")
            return false
        }
        if (ip == null) return false
        val cfg = IP_DEVICE_MAPPING[ip] ?: run {
            RealTimeLogger.i("AppConfig", "autoConfigureFromIp: unknown IP=$ip, skipping")
            return false
        }
        context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE).edit()
            .putString("terminal_id", cfg.terminalId)
            .putBoolean("terminal_identity_configured", true)
            .putString("edge_host", cfg.edgeUrl)
            .putBoolean("edge_endpoint_configured", true)
            .putBoolean("edge_ssid_port_rewrite", false)
            .putString(KEY_CONFIG_SOURCE, CONFIG_SOURCE_IP_AUTO)
            .commit()
        cachedTerminalId = cfg.terminalId
        RealTimeLogger.i("AppConfig", "autoConfigureFromIp: $ip → ${cfg.terminalId} / ${cfg.edgeUrl}")
        return true
    }

    /** アプリ起動時（と clear_manual_config の直後）に呼ぶ。手動設定があれば何もしない。 */
    fun autoConfigureFromIp(context: Context) {
        applyIpAutoConfig(context, getWifiIpAddress(context))
    }

    @Suppress("DEPRECATION")
    private fun getWifiIpAddress(context: Context): String? = try {
        val wm = context.applicationContext
            .getSystemService(android.content.Context.WIFI_SERVICE) as android.net.wifi.WifiManager
        val ip = wm.connectionInfo.ipAddress
        if (ip == 0) null
        else "%d.%d.%d.%d".format(ip and 0xff, (ip shr 8) and 0xff, (ip shr 16) and 0xff, (ip shr 24) and 0xff)
    } catch (_: Exception) { null }

    // --- Device Config Loader ---
    // Load device_config.json from assets if present, and cache into SharedPreferences.
    // This allows per-device configuration to be injected via assets at build time.
    fun loadDeviceConfig(context: Context) {
        try {
            val am = context.assets
            // Check for device_config.json or any device_config.*.json in assets
            val list = am.list("") ?: emptyArray()
            var deviceFileName: String? = null
            if (list.contains("device_config.json")) {
                deviceFileName = "device_config.json"
            } else {
                // pick the first device_config.<name>.json if present
                deviceFileName = list.firstOrNull { it.startsWith("device_config.") && it.endsWith(".json") }
            }
            if (deviceFileName == null) return

            val jsonStr = am.open(deviceFileName).bufferedReader().use { it.readText() }
            val jo = org.json.JSONObject(jsonStr)

            val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
            val editor = prefs.edit()

            // Map known keys to preferences
            if (jo.has("preferred_ssid")) editor.putString("router_ssid_a", jo.getString("preferred_ssid"))
            if (jo.has("upload_ssid")) editor.putString("router_ssid_b", jo.getString("upload_ssid"))
            // 同一 APK 用: assets の terminal_id が空なら上書きしない（初回画面で設定）
            if (jo.has("terminal_id")) {
                val tid = jo.optString("terminal_id", "").trim()
                if (tid.isNotEmpty()) {
                    editor.putString("terminal_id", tid)
                    cachedTerminalId = tid
                }
            }

            // Store raw config for other components to read
            editor.putString("device_config_raw", jsonStr)

            editor.apply()
            RealTimeLogger.i("AppConfig", "Loaded $deviceFileName: ${jo.optString("terminal_id")}")
        } catch (e: Exception) {
            RealTimeLogger.w("AppConfig", "Failed to load device_config.json: ${e.message}")
        }
    }

    // --- Client Config (client_config.json) Support ---
    private var cachedTerminalId: String? = null
    private var cachedCurrentEdgeId: String? = null
    private var cachedTargetEdgeId: String? = null
    private var cachedCurrentRouterId: String? = null
    private var cachedTargetRouterId: String? = null

    fun getTerminalId(context: Context): String {
        if (cachedTerminalId != null) return cachedTerminalId!!
        // prefer value stored in SharedPreferences by loadDeviceConfig
        try {
            val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
            val tid = prefs.getString("terminal_id", null)
            if (!tid.isNullOrBlank()) {
                cachedTerminalId = tid
                return tid
            }
        } catch (_: Exception) {}

        loadClientConfig(context)
        return cachedTerminalId ?: "device-001"
    }

    fun getCurrentEdgeId(context: Context): String {
        if (cachedCurrentEdgeId != null) return cachedCurrentEdgeId!!
        loadClientConfig(context)
        return cachedCurrentEdgeId ?: "edge-default"
    }

    /**
     * デモモードか。ConfigInjectionReceiver で `--ez demo_mode true` を注入したときのみ true。
     * 未設定（＝通常の研究運用）は false で、端末画面に DEMO バッジは出ない。
     */
    fun isDemoMode(context: Context): Boolean {
        return try {
            context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
                .getBoolean("demo_mode", false)
        } catch (_: Exception) {
            false
        }
    }

    fun getTargetEdgeId(context: Context): String {
        if (cachedTargetEdgeId != null) return cachedTargetEdgeId!!
        loadClientConfig(context)
        return cachedTargetEdgeId ?: getCurrentEdgeId(context)
    }

    fun getCurrentRouterId(context: Context): String {
        if (cachedCurrentRouterId != null) return cachedCurrentRouterId!!
        loadClientConfig(context)
        return cachedCurrentRouterId ?: "router-default"
    }

    fun getTargetRouterId(context: Context): String {
        if (cachedTargetRouterId != null) return cachedTargetRouterId!!
        loadClientConfig(context)
        return cachedTargetRouterId ?: getCurrentRouterId(context)
    }

    private fun loadClientConfig(context: Context) {
        try {
            val file = java.io.File(context.filesDir, "client_config.json")
            if (file.exists()) {
                val json = org.json.JSONObject(file.readText())
                cachedTerminalId = json.optString("terminal_id").takeIf { it.isNotEmpty() }
                cachedCurrentEdgeId = json.optString("current_edge_id").takeIf { it.isNotEmpty() }
                cachedTargetEdgeId = json.optString("target_edge_id").takeIf { it.isNotEmpty() }
                cachedCurrentRouterId = json.optString("current_router_id").takeIf { it.isNotEmpty() }
                cachedTargetRouterId = json.optString("target_router_id").takeIf { it.isNotEmpty() }
            }
        } catch (e: Exception) {
            // ignore
        }
    }

    // Allow runtime override to prefer query-style download URLs (e.g. /download?rel_path=...)
    // Store boolean in PREFS_ENV as "prefer_query_first" (default: false)
    fun getPreferQueryFirst(context: Context): Boolean {
        val prefs = context.getSharedPreferences(PREFS_ENV, Context.MODE_PRIVATE)
        return prefs.getBoolean("prefer_query_first", false)
    }
}

/** サーバ指定 > アプリ設定（未知のサーバ値は無視）。戻り値は (モード, 出所 "server"/"app")。 */
fun resolveBaselineMode(server: String?, app: String): Pair<String, String> =
    if (server != null && server in AppConfig.BASELINE_MODES) server to "server" else app to "app"

/** サーバ（/meta の hfl_mode）> アプリ設定（未知のサーバ値は無視）。戻り値は (モード, 出所 "server"/"app")。 */
fun resolveHflMode(server: String?, app: String): Pair<String, String> =
    if (server != null && server in AppConfig.HFL_MODES) server to "server" else app to "app"

/** SP3d: サーバ（/meta の num_cycles、1〜100）> アプリ設定（範囲外・null のサーバ値は無視）。戻り値は (サイクル数, 出所 "server"/"app")。 */
fun resolveNumCycles(server: Int?, app: Int): Pair<Int, String> =
    if (AppConfig.isValidNumCycles(server)) server!! to "server" else app to "app"
