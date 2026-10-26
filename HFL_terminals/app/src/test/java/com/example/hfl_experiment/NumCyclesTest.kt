package com.example.hfl_experiment

import android.content.Context
import android.content.Intent
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.debug.ConfigInjectionReceiver
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/** SP3d: 1 セッションのサイクル数はサーバ（/meta の num_cycles）を優先し、セッション開始時に固定する。 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class NumCyclesTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()

    @Before fun clear() {
        ctx.getSharedPreferences("env_prefs", Context.MODE_PRIVATE).edit().clear().commit()
        AppConfig.updateServerNumCycles(null)
        AppConfig.endSessionNumCycles()
        AppConfig.loadNumCycles(ctx)
    }

    @After fun tearDown() {
        // AppConfig は object（JVM 内で共有）なので、他のテストへ持ち越さない
        AppConfig.endSessionNumCycles()
        AppConfig.updateServerNumCycles(null)
        AppConfig.setNumCycles(ctx, AppConfig.DEFAULT_NUM_CYCLES)
    }

    // --- 純関数 ---
    @Test fun resolve_serverWins() = assertEquals(20 to "server", resolveNumCycles(20, 5))
    @Test fun resolve_appWhenServerMissing() = assertEquals(5 to "app", resolveNumCycles(null, 5))
    @Test fun resolve_outOfRangeServerIgnored() {
        assertEquals(5 to "app", resolveNumCycles(0, 5))
        assertEquals(5 to "app", resolveNumCycles(101, 5))
        assertEquals(100 to "server", resolveNumCycles(100, 5))
        assertEquals(1 to "server", resolveNumCycles(1, 5))
    }

    // --- アプリ設定 ---
    @Test fun default_isFive() {
        assertEquals(5, AppConfig.DEFAULT_NUM_CYCLES)
        assertEquals(5, AppConfig.appNumCycles)
        assertEquals(5, AppConfig.numCycles)
        assertEquals("app", AppConfig.numCyclesSource.value)
    }

    @Test fun set_isPersisted_andRangeChecked() {
        AppConfig.setNumCycles(ctx, 20)
        AppConfig.loadNumCycles(ctx)                       // 再起動相当
        assertEquals(20, AppConfig.appNumCycles)
        assertThrows(IllegalArgumentException::class.java) { AppConfig.setNumCycles(ctx, 0) }
        assertThrows(IllegalArgumentException::class.java) { AppConfig.setNumCycles(ctx, 101) }
        assertEquals(20, AppConfig.appNumCycles)
    }

    @Test fun storedOutOfRange_fallsBackToDefault() {
        ctx.getSharedPreferences("env_prefs", Context.MODE_PRIVATE).edit().putInt("num_cycles", 0).commit()
        AppConfig.loadNumCycles(ctx)
        assertEquals(5, AppConfig.appNumCycles)
    }

    @Test fun adbBroadcast_setsNumCycles_andRejectsOutOfRange() {
        ConfigInjectionReceiver().onReceive(ctx, Intent("com.example.hfl_experiment.SET_CONFIG").putExtra("num_cycles", 20))
        assertEquals(20, AppConfig.appNumCycles)
        ConfigInjectionReceiver().onReceive(ctx, Intent("com.example.hfl_experiment.SET_CONFIG").putExtra("num_cycles", 101))
        assertEquals(20, AppConfig.appNumCycles)
    }

    @Test fun serverValue_isShownOutsideSession() {
        AppConfig.updateServerNumCycles(3)
        assertEquals(3, AppConfig.numCycles)
        assertEquals("server", AppConfig.numCyclesSource.value)
        AppConfig.updateServerNumCycles(0)                 // 範囲外はアプリ設定へ
        assertEquals(5, AppConfig.numCycles)
    }

    @Test fun session_isFixedUntilNextSession() {
        AppConfig.setNumCycles(ctx, 5)
        assertEquals(5 to "app", AppConfig.beginSessionNumCycles())
        AppConfig.updateServerNumCycles(3)                  // セッション中にサーバが変更
        AppConfig.setNumCycles(ctx, 7)                      // セッション中に端末設定も変更
        assertEquals(5, AppConfig.numCycles)
        assertEquals("app", AppConfig.numCyclesSource.value)
        assertEquals(3, AppConfig.pendingNumCycles())
        AppConfig.endSessionNumCycles()
        assertEquals(3 to "server", AppConfig.beginSessionNumCycles())
        assertNull(AppConfig.pendingNumCycles())
    }
}
