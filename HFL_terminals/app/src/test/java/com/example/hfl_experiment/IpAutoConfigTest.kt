package com.example.hfl_experiment

import android.content.Context
import android.content.Intent
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.debug.ConfigInjectionReceiver
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/** SP3d 4.2: 手動設定（adb・設定画面・初回セットアップ画面）があれば、IP からの自動設定で上書きしない（R8）。 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class IpAutoConfigTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private fun prefs() = ctx.getSharedPreferences("env_prefs", Context.MODE_PRIVATE)
    private fun inject(vararg kv: Pair<String, Any>) = ConfigInjectionReceiver().onReceive(ctx,
        Intent("com.example.hfl_experiment.SET_CONFIG").apply {
            kv.forEach { (k, v) -> when (v) { is String -> putExtra(k, v); is Boolean -> putExtra(k, v); is Int -> putExtra(k, v) } }
        })

    @Before fun clear() { prefs().edit().clear().commit(); AppConfig.clearTerminalIdMemoryCache() }

    @After fun tearDown() {
        prefs().edit().clear().commit(); AppConfig.clearTerminalIdMemoryCache()
        AppConfig.loadBaselineMode(ctx); AppConfig.loadNumCycles(ctx)
    }

    @Test fun unset_appliesIpMapping() {
        assertTrue(AppConfig.applyIpAutoConfig(ctx, "192.168.11.13"))
        assertEquals("terminal-03", prefs().getString("terminal_id", null))
        assertEquals("http://192.168.11.6:8002", prefs().getString("edge_host", null))
        assertEquals("ip_auto", AppConfig.configSource(ctx))
    }

    @Test fun manualViaAdb_isNotOverwritten() {
        inject("terminal_id" to "terminal-09", "edge_url" to "http://10.0.0.5:8001")
        assertEquals("manual", AppConfig.configSource(ctx))
        assertFalse(AppConfig.applyIpAutoConfig(ctx, "192.168.11.11"))
        assertEquals("terminal-09", prefs().getString("terminal_id", null))
        assertEquals("http://10.0.0.5:8001", prefs().getString("edge_host", null))
    }

    @Test fun manualEdgeUrlOnly_isManual() {
        inject("edge_url" to "http://10.0.0.5:8001")
        assertEquals("manual", AppConfig.configSource(ctx))
    }

    @Test fun previousIpAuto_isRefreshed() {
        AppConfig.applyIpAutoConfig(ctx, "192.168.11.11")
        assertTrue(AppConfig.applyIpAutoConfig(ctx, "192.168.11.12"))
        assertEquals("terminal-02", prefs().getString("terminal_id", null))
    }

    @Test fun modeOnlyInjection_isNotManual() {
        inject("baseline_mode" to "fixed", "num_cycles" to 7, "demo_mode" to true)
        assertNull(AppConfig.configSource(ctx))
        assertTrue(AppConfig.applyIpAutoConfig(ctx, "192.168.11.11"))   // 次の起動で IP 自動設定が働く
        AppConfig.setBaselineMode(ctx, "none")
    }

    @Test fun clearManual_appliesIpImmediately() {
        AppConfig.markManualConfig(ctx)
        // 注入経路の IP は端末の Wi-Fi から取る（Robolectric では取れない）ので、解除は単体の関数で確認する
        inject("clear_manual_config" to true)
        assertFalse("manual" == AppConfig.configSource(ctx))
        assertTrue(AppConfig.applyIpAutoConfig(ctx, "192.168.11.14"))
        assertEquals("terminal-04", prefs().getString("terminal_id", null))
        assertEquals("ip_auto", AppConfig.configSource(ctx))
    }

    @Test fun clearManual_thenInjectedIdentity_isManualAgain() {
        AppConfig.markManualConfig(ctx)
        inject("clear_manual_config" to true, "terminal_id" to "terminal-07")
        assertEquals("manual", AppConfig.configSource(ctx))
        assertEquals("terminal-07", prefs().getString("terminal_id", null))
    }

    @Test fun terminalIdentityScreen_marksManual() {
        AppConfig.applyTerminalIdentity(ctx, "terminal-05", "g")
        assertEquals("manual", AppConfig.configSource(ctx))
        assertFalse(AppConfig.applyIpAutoConfig(ctx, "192.168.11.11"))
        assertEquals("terminal-05", prefs().getString("terminal_id", null))
    }

    @Test fun unknownIp_doesNothing() {
        assertFalse(AppConfig.applyIpAutoConfig(ctx, "10.1.2.3"))
        assertFalse(AppConfig.applyIpAutoConfig(ctx, null))
        assertNull(AppConfig.configSource(ctx))
    }
}
