package com.example.hfl_experiment

import android.content.Context
import android.content.Intent
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.debug.ConfigInjectionReceiver
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/** central モードの問い合わせ先（centralized-mode 5.6）。adb で与える。未設定は空（問い合わせない）。 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class CentralUrlConfigTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()

    @Before fun clear() { ctx.getSharedPreferences("env_prefs", Context.MODE_PRIVATE).edit().clear().commit() }

    @Test fun default_isBlank() = assertEquals("", AppConfig.getCentralUrl(ctx))

    @Test fun adbBroadcast_setsUrl_trimmed() {
        ConfigInjectionReceiver().onReceive(ctx, Intent("com.example.hfl_experiment.SET_CONFIG").putExtra("central_url", "  http://192.168.11.2:8000/ "))
        assertEquals("http://192.168.11.2:8000", AppConfig.getCentralUrl(ctx))
    }

    @Test fun adbBroadcast_withoutKey_keepsExisting() {
        AppConfig.setCentralUrl(ctx, "http://10.0.2.2:8000")
        ConfigInjectionReceiver().onReceive(ctx, Intent("com.example.hfl_experiment.SET_CONFIG").putExtra("baseline_mode", "central"))
        assertEquals("http://10.0.2.2:8000", AppConfig.getCentralUrl(ctx))
    }

    @Test fun blankValue_clears() {
        AppConfig.setCentralUrl(ctx, "http://10.0.2.2:8000")
        AppConfig.setCentralUrl(ctx, "   ")
        assertEquals("", AppConfig.getCentralUrl(ctx))
    }
}
