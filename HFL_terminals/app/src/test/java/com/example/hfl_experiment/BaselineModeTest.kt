package com.example.hfl_experiment

import android.content.Context
import android.content.Intent
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.debug.ConfigInjectionReceiver
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class BaselineModeTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()

    @Before fun clear() {
        ctx.getSharedPreferences("env_prefs", Context.MODE_PRIVATE).edit().clear().commit()
        AppConfig.updateServerBaselineMode(null)
        AppConfig.endSessionBaselineMode()
        AppConfig.loadBaselineMode(ctx)
    }

    @org.junit.After fun tearDown() {
        // AppConfig は object（JVM 内で共有）なので、セッション状態・サーバ指定を他のテストへ持ち越さない
        AppConfig.endSessionBaselineMode()
        AppConfig.updateServerBaselineMode(null)
    }

    @Test fun default_isNormalHflTraining() = assertEquals("none", AppConfig.BASELINE_MODE)

    @Test fun setMode_isAppliedAndPersisted() {
        AppConfig.setBaselineMode(ctx, "fixed")
        assertEquals("fixed", AppConfig.BASELINE_MODE)
        AppConfig.loadBaselineMode(ctx)   // 再起動相当
        assertEquals("fixed", AppConfig.BASELINE_MODE)
    }

    @Test fun invalidMode_isRejected_andKeepsCurrent() {
        AppConfig.setBaselineMode(ctx, "random")
        assertThrows(IllegalArgumentException::class.java) { AppConfig.setBaselineMode(ctx, "bogus") }
        assertEquals("random", AppConfig.BASELINE_MODE)
    }

    @Test fun flow_followsChanges_forUiDisplay() {
        AppConfig.setBaselineMode(ctx, "fixed")
        assertEquals("fixed", AppConfig.baselineModeFlow.value)
        AppConfig.setBaselineMode(ctx, "none")
        assertEquals("none", AppConfig.baselineModeFlow.value)
    }

    @Test fun adbBroadcast_setsMode() {
        ConfigInjectionReceiver().onReceive(ctx, Intent("com.example.hfl_experiment.SET_CONFIG").putExtra("baseline_mode", "greedy"))
        assertEquals("greedy", AppConfig.BASELINE_MODE)
    }

    @Test fun resolve_serverWins_unknownIgnored() {
        assertEquals("fixed" to "server", resolveBaselineMode("fixed", "none"))
        assertEquals("none" to "app", resolveBaselineMode(null, "none"))
        assertEquals("random" to "app", resolveBaselineMode("bogus", "random"))
    }

    @Test fun sessionMode_isFixedUntilNextSession() {
        AppConfig.setBaselineMode(ctx, "none")
        assertEquals("none" to "none", AppConfig.beginSessionBaselineMode(ctx))
        AppConfig.updateServerBaselineMode("fixed")          // セッション中にサーバが変更
        assertEquals("none", AppConfig.BASELINE_MODE)        // 現在セッションは変わらない
        assertEquals("fixed", AppConfig.pendingBaselineMode())
        AppConfig.setBaselineMode(ctx, "greedy")             // セッション中のアプリ設定変更も同様
        assertEquals("none", AppConfig.BASELINE_MODE)
        AppConfig.endSessionBaselineMode()
        assertEquals("none" to "fixed", AppConfig.beginSessionBaselineMode(ctx))  // 次セッション（サーバ優先）
        assertEquals("fixed", AppConfig.BASELINE_MODE)
        assertEquals("server", AppConfig.baselineModeSource.value)
        assertNull(AppConfig.pendingBaselineMode())
    }

    @Test fun previousSessionMode_isPersisted() {
        AppConfig.setBaselineMode(ctx, "random")
        AppConfig.beginSessionBaselineMode(ctx)
        AppConfig.endSessionBaselineMode()
        AppConfig.loadBaselineMode(ctx)                       // 再起動相当
        AppConfig.setBaselineMode(ctx, "none")
        assertEquals("random" to "none", AppConfig.beginSessionBaselineMode(ctx))
    }

    // --- centralized-mode: central（学習せず中央へ問い合わせる対照群） ---
    @Test fun central_isAcceptedByAppSetting_andAdb_andServer() {
        AppConfig.setBaselineMode(ctx, "central")
        assertEquals("central", AppConfig.BASELINE_MODE)
        AppConfig.setBaselineMode(ctx, "none")
        ConfigInjectionReceiver().onReceive(ctx, Intent("com.example.hfl_experiment.SET_CONFIG").putExtra("baseline_mode", "central"))
        assertEquals("central", AppConfig.BASELINE_MODE)
        assertEquals("central" to "server", resolveBaselineMode("central", "none"))
    }

    @Test fun nearMissOfCentral_isStillRejected() {
        assertThrows(IllegalArgumentException::class.java) { AppConfig.setBaselineMode(ctx, "central2") }
        assertEquals("none" to "app", resolveBaselineMode("central2", "none"))
    }

    // --- hfl-recommended-ap-mode 4.3: hfl_rec ---
    @Test fun hflRec_isAcceptedByAppSetting_andAdb_andServer() {
        AppConfig.setBaselineMode(ctx, "hfl_rec")
        assertEquals("hfl_rec", AppConfig.BASELINE_MODE)
        AppConfig.setBaselineMode(ctx, "none")
        ConfigInjectionReceiver().onReceive(ctx, Intent("com.example.hfl_experiment.SET_CONFIG").putExtra("baseline_mode", "hfl_rec"))
        assertEquals("hfl_rec", AppConfig.BASELINE_MODE)
        assertEquals("hfl_rec" to "server", resolveBaselineMode("hfl_rec", "none"))
    }

    @Test fun hflRec_nearMissIsRejected() {
        assertThrows(IllegalArgumentException::class.java) { AppConfig.setBaselineMode(ctx, "hfl_rec2") }
        assertEquals("none" to "app", resolveBaselineMode("hfl_rec2", "none"))
    }

    // --- fix-audit-20261026: fixed_ap1 ---
    @Test fun fixedAp1_isAcceptedByAppSetting_andAdb_andServer() {
        AppConfig.setBaselineMode(ctx, "fixed_ap1")
        assertEquals("fixed_ap1", AppConfig.BASELINE_MODE)
        AppConfig.setBaselineMode(ctx, "none")
        ConfigInjectionReceiver().onReceive(ctx, Intent("com.example.hfl_experiment.SET_CONFIG").putExtra("baseline_mode", "fixed_ap1"))
        assertEquals("fixed_ap1", AppConfig.BASELINE_MODE)
        assertEquals("fixed_ap1" to "server", resolveBaselineMode("fixed_ap1", "none"))
    }

    @Test fun fixedAp2_isRejected() {
        assertThrows(IllegalArgumentException::class.java) { AppConfig.setBaselineMode(ctx, "fixed_ap2") }
        assertEquals("none" to "app", resolveBaselineMode("fixed_ap2", "none"))
    }
}
