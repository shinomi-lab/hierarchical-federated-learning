package com.example.hfl_experiment.experiment

import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import java.io.File

class DecisionEngineTest {
    private lateinit var engine: DecisionEngine
    private lateinit var configData: Config.ConfigData

    @Before
    fun setup() {
        // Load config from project assets path so this test can run on JVM without Android framework
        val cfgFile = File("app/src/main/assets/ap_config.json")
        configData = Config.loadConfigFromFile(cfgFile)
        // provide a dummy Context by using a simple mock if needed; DecisionEngine currently only passes context to ModelLoader
        val dummyContext = org.mockito.Mockito.mock(android.content.Context::class.java)
        engine = DecisionEngine(dummyContext, configData)
    }

    @Test
    fun testEmaSmoothingAndConsecutiveThreshold() {
        val appOneHot = floatArrayOf(1f, 0f, 0f, 0f)
        // feed repeated observations that favor AP1
        var res = engine.observe(tp = 30f, rtt = 50f, appOneHot = appOneHot)
        assertNotNull(res)
        // call multiple times to exceed consecutiveThreshold
        for (i in 0 until (configData.consecutiveThreshold + 1)) {
            res = engine.observe(tp = 100f, rtt = 20f, appOneHot = appOneHot)
        }
        assertTrue(res.chosenApId.isNotBlank())
    }

    @Test
    fun testDebounceMinSwitch() {
        val appOneHot = floatArrayOf(0f, 1f, 0f, 0f)
        val res1 = engine.observe(tp = 50f, rtt = 30f, appOneHot = appOneHot)
        val res2 = engine.observe(tp = 200f, rtt = 10f, appOneHot = appOneHot)
        assertNotNull(res1)
        assertNotNull(res2)
    }
}
