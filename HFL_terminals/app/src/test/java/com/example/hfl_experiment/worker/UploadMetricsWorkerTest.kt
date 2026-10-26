package com.example.hfl_experiment.worker

import androidx.test.core.app.ApplicationProvider
import com.example.hfl_experiment.network.NetworkClient
import com.example.hfl_experiment.training.TrainingMetricsStore
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class UploadMetricsWorkerTest {
    @get:Rule val tmp = TemporaryFolder()
    private val server = MockWebServer()

    @Before fun setUp() { server.start(); TrainingMetricsStore.initDir(tmp.newFolder()) }
    @After fun tearDown() { server.shutdown() }

    private fun client() = NetworkClient(ApplicationProvider.getApplicationContext(), server.url("/").toString())
    private fun row(id: String) = JSONObject().put("data_id", id).put("terminal_id", "t1").put("epoch_val_accuracy", org.json.JSONArray(listOf(0.1, 0.2)))

    @Test fun sendMetricJson_postsBodyAsIs() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200))
        assertTrue(client().sendMetricJson(row("a").toString()))
        val req = server.takeRequest()
        assertEquals("/upload_training_metrics", req.path)
        assertTrue(req.getHeader("Content-Type")!!.startsWith("application/json"))
        assertEquals(2, JSONObject(req.body.readUtf8()).getJSONArray("epoch_val_accuracy").length())
    }

    @Test fun sendMetricJson_502_isFalse() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(502))
        assertFalse(client().sendMetricJson(row("a").toString()))
    }

    @Test fun drain_allOk_emptiesOutbox_keepsLedger() = runBlocking {
        TrainingMetricsStore.recordRow(row("a")); TrainingMetricsStore.recordRow(row("b"))
        val sent = mutableListOf<String>()
        assertTrue(UploadMetricsWorker.drainOutbox { sent += it.getString("data_id"); true })
        assertEquals(listOf("a", "b"), sent)
        assertTrue(TrainingMetricsStore.pending().isEmpty())
        assertEquals(setOf("a", "b"), TrainingMetricsStore.latestRows().keys)
    }

    @Test fun drain_failureMidway_keepsUnsent() = runBlocking {
        TrainingMetricsStore.recordRow(row("a")); TrainingMetricsStore.recordRow(row("b"))
        assertFalse(UploadMetricsWorker.drainOutbox { it.getString("data_id") == "a" })
        assertEquals(listOf("b"), TrainingMetricsStore.pending().map { it.getString("data_id") })
    }

    @Test fun drain_picksUpRowsRecordedDuringSend() = runBlocking {
        TrainingMetricsStore.recordRow(row("a"))
        val sent = mutableListOf<String>()
        assertTrue(UploadMetricsWorker.drainOutbox {
            sent += it.getString("data_id")
            if (it.getString("data_id") == "a") TrainingMetricsStore.recordRow(row("b"))
            true
        })
        assertEquals(listOf("a", "b"), sent)
        assertTrue(TrainingMetricsStore.pending().isEmpty())
    }

    @Test fun sendRowJson_routesExperimentEventsByKind() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200)); server.enqueue(MockResponse().setResponseCode(200))
        assertTrue(client().sendRowJson(JSONObject().put("kind", "experiment_event").put("type", "baseline_mode_change")))
        assertTrue(client().sendRowJson(row("m1")))
        assertEquals("/api/v1/experiment_events", server.takeRequest().path)
        assertEquals("/upload_training_metrics", server.takeRequest().path)
    }
}
