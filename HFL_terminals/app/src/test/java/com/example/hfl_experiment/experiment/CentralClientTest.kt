package com.example.hfl_experiment.experiment

import com.google.gson.JsonParser
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.TimeUnit

/** 中央への問い合わせクライアント（Android 依存なし）。応答の解釈・失敗の分類・往復時間（centralized-mode D5/D6）。 */
class CentralClientTest {
    private lateinit var server: MockWebServer

    @Before fun start() { server = MockWebServer().also { it.start() } }
    @After fun stop() { try { server.shutdown() } catch (_: Exception) {} }

    private fun url() = server.url("/").toString().trimEnd('/')

    private fun okBody(ap: Int = 1) =
        """{"chosen_ap":$ap,"scores":[0.2,0.8],"model_sha256":"abc123","server_ms":1.5}"""

    @Test fun ok_parsesResponseAndSendsTerminalAndApp() {
        server.enqueue(MockResponse().setBody(okBody(1)))
        val out = CentralClient(url()).query("terminal-02", "動画")
        assertEquals(CentralClient.STATUS_OK, out.status)
        assertEquals(1, out.chosenAp)
        assertEquals(listOf(0.2, 0.8), out.scores)
        assertEquals("abc123", out.modelSha256)
        assertEquals(1.5, out.serverMs!!, 1e-9)
        assertEquals(url() + "/api/v1/central_infer", out.url)

        val req = server.takeRequest(1, TimeUnit.SECONDS)!!
        assertEquals("POST", req.method)
        assertEquals("/api/v1/central_infer", req.path)
        val body = JsonParser.parseString(req.body.readUtf8()).asJsonObject
        assertEquals("terminal-02", body["terminal_id"].asString)
        assertEquals("動画", body["app_type"].asString)
        assertEquals(2, body.size())   // 実測 TP/RTT は送らない
    }

    @Test fun rtt_isMeasuredWithInjectedClock() {
        server.enqueue(MockResponse().setBody(okBody()))
        val ticks = longArrayOf(1_000_000_000L, 1_042_000_000L).iterator()   // 42 ms
        val out = CentralClient(url(), nanoTime = { ticks.next() }).query("t", "動画")
        assertEquals(42L, out.rttMs)
    }

    @Test fun urlWithTrailingSlash_isAccepted() {
        server.enqueue(MockResponse().setBody(okBody()))
        assertEquals(CentralClient.STATUS_OK, CentralClient(url() + "/").query("t", "動画").status)
    }

    @Test fun serverError_5xx_isClassified_andKeepsNoAp() {
        server.enqueue(MockResponse().setResponseCode(503).setBody("""{"detail":"central_model が用意されていません"}"""))
        val out = CentralClient(url()).query("t", "動画")
        assertEquals(CentralClient.STATUS_SERVER_ERROR, out.status)
        assertEquals(503, out.httpCode)
        assertNull(out.chosenAp)
    }

    @Test fun badRequest_4xx_isClassified() {
        server.enqueue(MockResponse().setResponseCode(400).setBody("""{"detail":"未知のアプリ種別です"}"""))
        val out = CentralClient(url()).query("t", "存在しない")
        assertEquals(CentralClient.STATUS_BAD_REQUEST, out.status)
        assertEquals(400, out.httpCode)
        assertNull(out.chosenAp)
    }

    @Test fun invalidBody_isBadResponse() {
        for (body in listOf("not json", "{}", """{"chosen_ap":"x","scores":[1],"model_sha256":"a"}""", """{"chosen_ap":-1,"scores":[1],"model_sha256":"a"}""")) {
            server.enqueue(MockResponse().setBody(body))
            val out = CentralClient(url()).query("t", "動画")
            assertEquals("body=$body", CentralClient.STATUS_BAD_RESPONSE, out.status)
            assertNull(out.chosenAp)
        }
    }

    @Test fun noAnswerWithinTimeout_isTimeout() {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        val out = CentralClient(url(), timeoutMs = 300).query("t", "動画")
        assertEquals(CentralClient.STATUS_TIMEOUT, out.status)
        assertNull(out.chosenAp)
        assertTrue("rtt=${out.rttMs}", out.rttMs in 250..3000)
    }

    @Test fun connectionRefused_isUnreachable() {
        val dead = url()
        server.shutdown()
        val out = CentralClient(dead, timeoutMs = 1000).query("t", "動画")
        assertEquals(CentralClient.STATUS_UNREACHABLE, out.status)
        assertNull(out.chosenAp)
    }

    @Test fun blankUrl_isNotConfigured_andSendsNothing() {
        val out = CentralClient("  ").query("t", "動画")
        assertEquals(CentralClient.STATUS_NOT_CONFIGURED, out.status)
        assertEquals(0, server.requestCount)
    }

    @Test fun defaultTimeout_is5Seconds() = assertEquals(5000L, CentralClient.DEFAULT_TIMEOUT_MS)

    // --- hfl-recommended-ap-mode 4.1: 問い合わせ先のパスと round / is_initial ---
    @Test fun hflRec_path_isConfigurable_andResponseCarriesRoundAndIsInitial() {
        server.enqueue(MockResponse().setBody("""{"chosen_ap":0,"scores":[0.9,0.1],"model_sha256":"g1","round":4,"is_initial":false,"server_ms":2.0}"""))
        val c = CentralClient(url(), path = CentralClient.HFL_REC_PATH)
        val out = c.query("terminal-01", "通話")
        assertEquals(CentralClient.STATUS_OK, out.status)
        assertEquals(url() + "/api/v1/hfl_rec_infer", out.url)
        assertEquals(url() + "/api/v1/hfl_rec_infer", c.endpointUrl)
        assertEquals(4, out.round); assertEquals(false, out.isInitial); assertEquals("g1", out.modelSha256)
        assertEquals("/api/v1/hfl_rec_infer", server.takeRequest(1, TimeUnit.SECONDS)!!.path)
    }

    @Test fun defaultPath_isCentralInfer_andRoundIsNullWhenAbsent() {
        server.enqueue(MockResponse().setBody(okBody()))
        val out = CentralClient(url()).query("t", "動画")
        assertEquals("/api/v1/central_infer", CentralClient.PATH)
        assertNull(out.round); assertNull(out.isInitial)
    }
}
