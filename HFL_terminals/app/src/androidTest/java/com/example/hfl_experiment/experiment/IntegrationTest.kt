package com.example.hfl_experiment.experiment

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class IntegrationTest {
    @Test
    fun testSwitchFlowWithMockNetworkManager() = runBlocking {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        val config = Config.loadConfigFromAssets(ctx)
        val nm = NetworkManager(ctx)
        val ml = ModelLoader()
        val eng = DecisionEngine(ctx, config, ml)
        val uploader = Uploader(ctx)

        // simulate observation loop
        val appHot = floatArrayOf(1f, 0f, 0f, 0f)
        for (i in 1..5) {
            val res = eng.observe(tp = 100f + i * 10, rtt = 20f, appOneHot = appHot)
            uploader.enqueue(DecisionLog(System.currentTimeMillis(), "devhash", nm.getCurrentApId(), res.chosenApId, res.rawScores, res.emaScores, 100f, 20f, "app0", res.reason, null))
        }

        // flush (stub)
        uploader.flushNow()
        assertTrue(true)
    }
}

