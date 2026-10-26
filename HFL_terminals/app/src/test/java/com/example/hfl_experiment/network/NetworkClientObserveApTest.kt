package com.example.hfl_experiment.network

import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** record-connected-ap 2.4: 実際の AP と実測をエッジに送る。失敗しても例外を投げず、計測を止めない。 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33], manifest = Config.NONE)
class NetworkClientObserveApTest {
    private lateinit var server: MockWebServer
    private lateinit var baseUrl: String

    @Before fun setup() { server = MockWebServer().also { it.start() }; baseUrl = server.url("/").toString().removeSuffix("/") }
    @After fun teardown() { server.shutdown() }

    @Test fun observeAp_postsTerminalApAndMeasurements() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"ok":true}"""))
        NetworkClient(ApplicationProvider.getApplicationContext(), baseUrl).observeAp("terminal-03", "AP1", 5.9, 40.0)
        val req = server.takeRequest()
        assertEquals("/api/v1/ap_states/observe", req.path)
        assertEquals("POST", req.method)
        val body = JSONObject(req.body.readUtf8())
        assertEquals("terminal-03", body.getString("terminal_id"))
        assertEquals("AP1", body.getString("ap"))
        assertEquals(5.9, body.getDouble("tp_mbps"), 1e-9)
        assertEquals(40.0, body.getDouble("rtt_ms"), 1e-9)
    }

    @Test fun observeAp_omitsMissingValues() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"ok":true}"""))
        NetworkClient(ApplicationProvider.getApplicationContext(), baseUrl).observeAp("terminal-01", "AP0", 11.0, null)
        val body = JSONObject(server.takeRequest().body.readUtf8())
        assertTrue(body.has("tp_mbps")); assertFalse(body.has("rtt_ms"))
    }

    @Test fun observeAp_doesNotThrowWhenServerIsDown() = runBlocking {
        val dead = baseUrl
        server.shutdown()
        NetworkClient(ApplicationProvider.getApplicationContext(), dead).observeAp("terminal-01", "AP0", 11.0, 3.0)
        // 例外が出ずにここへ来ること自体が検証（計測を止めない）
        assertTrue(true)
    }

    @Test fun observeAp_doesNotThrowOn400() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(400).setBody("""{"detail":"Unknown AP"}"""))
        NetworkClient(ApplicationProvider.getApplicationContext(), baseUrl).observeAp("terminal-01", "AP9", 1.0, 1.0)
        assertEquals(1, server.requestCount)
    }

    // --- fetchApStateInfos（3.1）: measured を読む。旧形式でも読める ---
    @Test fun fetchApStateInfos_readsMeasuredAndModel() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200).setBody(
            """{"AP0":{"tp_mbps":11.7,"rtt_ms":3.0,"n_connected":2,"measured":{"tp_mbps":11.0,"rtt_ms":3.5,"n":2,"age_s":4.0},"ap_states_source":"central"},
               "AP1":{"tp_mbps":5.9,"rtt_ms":6.7,"n_connected":0,"measured":null,"ap_states_source":"central"}}"""))
        val got = NetworkClient(ApplicationProvider.getApplicationContext(), baseUrl).fetchApStateInfos()!!
        assertEquals(setOf("AP0", "AP1"), got.keys)
        assertEquals(11.0f, got["AP0"]!!.measuredTp!!, 0f)
        assertEquals(3.5f, got["AP0"]!!.measuredRtt!!, 0f)
        assertEquals(5.9f, got["AP1"]!!.tp, 0f)
        assertEquals(null, got["AP1"]!!.measuredTp)
    }

    @Test fun fetchApStateInfos_readsOldFormatWithoutMeasured() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200).setBody(
            """{"AP0":{"tp_mbps":59.4,"rtt_ms":117.6,"n_connected":4},"AP2":{"tp_mbps":20.0,"rtt_ms":200.0,"n_connected":0}}"""))
        val got = NetworkClient(ApplicationProvider.getApplicationContext(), baseUrl).fetchApStateInfos()!!
        assertEquals(59.4f, got["AP0"]!!.tp, 0.001f)
        assertEquals(null, got["AP0"]!!.measuredRtt)
    }

    @Test fun fetchApStateInfos_nullOnServerError() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(500))
        assertEquals(null, NetworkClient(ApplicationProvider.getApplicationContext(), baseUrl).fetchApStateInfos())
    }
}
