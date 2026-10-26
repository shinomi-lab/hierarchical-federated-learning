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
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/** SP3b R1: 端末の HFL モード（sync/async）はサーバ（/meta の hfl_mode）を優先し、セッション開始時に固定する。 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class HflModeResolveTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()

    @Before fun clear() {
        ctx.getSharedPreferences("env_prefs", Context.MODE_PRIVATE).edit().clear().commit()
        AppConfig.updateServerHflMode(null)
        AppConfig.endSessionHflMode()
        AppConfig.loadHflMode(ctx)
    }

    @After fun tearDown() {
        // AppConfig は object（JVM 内で共有）なので、他のテストへ持ち越さない
        AppConfig.endSessionHflMode()
        AppConfig.updateServerHflMode(null)
        AppConfig.setHflMode(ctx, AppConfig.DEFAULT_HFL_MODE)
    }

    // --- 純関数 ---
    @Test fun serverWins() = assertEquals("async" to "server", resolveHflMode("async", "sync"))
    @Test fun appWhenServerMissing() = assertEquals("async" to "app", resolveHflMode(null, "async"))
    @Test fun invalidServerIgnored() = assertEquals("sync" to "app", resolveHflMode("weird", "sync"))

    // --- アプリ設定 ---
    @Test fun default_isSync() {
        assertEquals("sync", AppConfig.DEFAULT_HFL_MODE)
        assertEquals("sync", AppConfig.appHflMode)
        assertEquals("sync", AppConfig.hflMode)
        assertFalse(AppConfig.isAsyncMode())
    }

    @Test fun setMode_isPersisted() {
        AppConfig.setHflMode(ctx, "async")
        AppConfig.loadHflMode(ctx)   // 再起動相当
        assertEquals("async", AppConfig.appHflMode)
        assertTrue(AppConfig.isAsyncMode())
    }

    @Test fun invalidMode_isRejected() {
        AppConfig.setHflMode(ctx, "async")
        assertThrows(IllegalArgumentException::class.java) { AppConfig.setHflMode(ctx, "bogus") }
        assertEquals("async", AppConfig.appHflMode)
    }

    @Test fun adbBroadcast_setsMode() {
        ConfigInjectionReceiver().onReceive(ctx, Intent("com.example.hfl_experiment.SET_CONFIG").putExtra("hfl_mode", "async"))
        assertEquals("async", AppConfig.appHflMode)
    }

    // --- セッション ---
    @Test fun session_serverWins_andMismatchReported() {
        AppConfig.setHflMode(ctx, "async")
        AppConfig.updateServerHflMode("sync")
        val (mode, source, mismatchApp) = AppConfig.beginSessionHflMode()
        assertEquals("sync", mode)
        assertEquals("server", source)
        assertEquals("async", mismatchApp)       // 食い違い時のみアプリ設定の値を返す
        assertFalse(AppConfig.isAsyncMode())
        assertEquals("server", AppConfig.hflModeSource.value)
    }

    @Test fun session_noMismatchWhenSame() {
        AppConfig.updateServerHflMode("async")
        AppConfig.setHflMode(ctx, "async")
        assertEquals(Triple("async", "server", null), AppConfig.beginSessionHflMode())
    }

    @Test fun session_appUsedWhenServerSilent() {
        AppConfig.setHflMode(ctx, "async")
        assertEquals(Triple("async", "app", null), AppConfig.beginSessionHflMode())
        assertTrue(AppConfig.isAsyncMode())
    }

    @Test fun sessionMode_isFixedUntilNextSession() {
        AppConfig.beginSessionHflMode()
        assertEquals("sync", AppConfig.hflMode)
        AppConfig.updateServerHflMode("async")    // セッション中にサーバ（再起動）が変わっても
        AppConfig.setHflMode(ctx, "async")        // アプリ設定を変えても
        assertEquals("sync", AppConfig.hflMode)   // 現在セッションは変わらない
        assertEquals("async", AppConfig.pendingHflMode())
        AppConfig.endSessionHflMode()
        assertEquals("async" to "server", AppConfig.beginSessionHflMode().let { it.first to it.second })
        assertNull(AppConfig.pendingHflMode())
    }
}
