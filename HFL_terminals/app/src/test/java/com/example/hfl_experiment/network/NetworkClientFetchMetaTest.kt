package com.example.hfl_experiment.network

import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Policy ③ (c): Android Context が必要なクラスを Robolectric で検証。
 *
 * NetworkClient.fetchMeta は実機の edge_server.endpoints.terminal_update.get_meta() が
 * 返す JSON 形式と整合しないと動かない。Robolectric + MockWebServer で
 * 実機サーバ起動なしに HTTP 層全体を検証できる。
 *
 * 実行: ./gradlew :app:testDebugUnitTest --tests *NetworkClientFetchMeta*
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33], manifest = Config.NONE)
class NetworkClientFetchMetaTest {

    private lateinit var server: MockWebServer
    private lateinit var baseUrl: String

    @Before
    fun setup() {
        server = MockWebServer()
        server.start()
        baseUrl = server.url("/").toString().removeSuffix("/")
    }

    @After
    fun teardown() {
        server.shutdown()
    }

    @Test
    fun fetchMeta_returnsParsedMeta_whenServerReturns200() = runBlocking {
        // 実機 edge_server.endpoints.terminal_update L1413 の get_meta レスポンスを再現
        val body = """
            {
              "edge_id": "edge-server-01",
              "round": 7,
              "model_id": "demo-mlp-v1",
              "base_hash": "sha256:bootstrap",
              "preferred_dtype": "torch_state_dict",
              "upload_endpoint": "/receive_terminal_weights/{terminal_id}",
              "aggregation_threshold": 2,
              "expected_param_len": 0,
              "order_version": 1
            }
        """.trimIndent()
        server.enqueue(MockResponse().setResponseCode(200).setBody(body))

        val ctx = ApplicationProvider.getApplicationContext<android.content.Context>()
        val nc = NetworkClient(context = ctx, baseUrl = baseUrl, authToken = "test-token")

        val meta = nc.fetchMeta(forceRefresh = true, ignoreThrottle = true)
        assertEquals(7, meta.round)
        assertEquals("demo-mlp-v1", meta.model_id)
        assertEquals("sha256:bootstrap", meta.base_hash)

        // 実際にサーバが受け取ったリクエストが /api/v1/meta だったことを確認
        val req = server.takeRequest()
        assertEquals("GET", req.method)
        assertTrue(
            "expected path to be /api/v1/meta, got ${req.path}",
            req.path?.startsWith("/api/v1/meta") == true,
        )
    }

    @Test
    fun fetchMeta_retriesOnTransient5xx() = runBlocking {
        // 503 → 503 → 200 のシーケンスでリトライが動くか
        server.enqueue(MockResponse().setResponseCode(503).setBody("{\"detail\":\"busy\"}"))
        server.enqueue(MockResponse().setResponseCode(503).setBody("{\"detail\":\"busy\"}"))
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"edge_id":"e","round":1,"model_id":"m","base_hash":"sha256:b","preferred_dtype":"torch_state_dict","upload_endpoint":"/x","aggregation_threshold":1,"expected_param_len":0,"order_version":1}"""
            )
        )

        val ctx = ApplicationProvider.getApplicationContext<android.content.Context>()
        val nc = NetworkClient(
            context = ctx, baseUrl = baseUrl,
            maxRetries = 3, initialBackoffMs = 100L, authToken = "t",
        )
        val meta = nc.fetchMeta(forceRefresh = true, ignoreThrottle = true)
        assertEquals(1, meta.round)
        // 3 回叩いていることを確認 (初回 + 2 リトライ + 成功)
        assertEquals(3, server.requestCount)
    }
}
