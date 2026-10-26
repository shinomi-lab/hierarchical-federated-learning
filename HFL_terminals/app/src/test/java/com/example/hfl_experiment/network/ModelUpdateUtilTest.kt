package com.example.hfl_experiment.network

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.training.core.LocalTrainer
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config
import java.io.File

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class ModelUpdateUtilTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private val server = MockWebServer()
    private fun res(p: String) = File(javaClass.classLoader!!.getResource(p)!!.toURI())
    private var serveMeta = true

    @Before fun setUp() {
        File(ctx.filesDir, "model").deleteRecursively()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val rel = request.requestUrl?.queryParameter("rel_path") ?: return MockResponse().setResponseCode(404)
                return when {
                    rel == "b1/weight.bin" -> MockResponse().setBody(Buffer().write(res("golden/format/weight.bin").readBytes()))
                    rel == "b1/meta.json" && serveMeta -> MockResponse().setBody(res("golden/format/meta.json").readText())
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
        server.start()
    }

    @After fun tearDown() = server.shutdown()

    private fun edgeBase() = server.url("/").toString().trimEnd('/')

    @Test fun siblingRef_handlesAllRefForms() {
        assertEquals("b1/meta.json", ModelUpdateUtil.siblingRef("b1/global_model_mobile.pt", "meta.json"))
        assertEquals("http://h:8001/download_model/b1/weight.bin",
            ModelUpdateUtil.siblingRef("http://h:8001/download_model/b1/global_model_mobile.pt", "weight.bin"))
        assertEquals("http://h:8001/download?rel_path=b1/meta.json",
            ModelUpdateUtil.siblingRef("http://h:8001/download?rel_path=b1/weight.bin", "meta.json"))
    }

    @Test fun ptRef_downloadsSiblingBinAndMeta_intoTrainerLocation() = runBlocking {
        val (ok, msg) = ModelUpdateUtil.downloadAndApplyModelSet(
            ctx, server.url("/download").toString(), OkHttpClient(), null, "b1/global_model_mobile.pt", null)
        assertTrue(msg, ok)
        assertArrayEquals(res("golden/format/weight.bin").readBytes(), LocalTrainer.downloadedModelFile(ctx.filesDir).readBytes())
        assertTrue(File(ctx.filesDir, "model/meta.json").isFile)
    }

    @Test fun missingMeta_stillAppliesBin_andClearsStaleMeta() = runBlocking {
        File(ctx.filesDir, "model").mkdirs()
        File(ctx.filesDir, "model/meta.json").writeText("{\"stale\":true}")
        serveMeta = false
        val (ok, msg) = ModelUpdateUtil.downloadAndApplyModelSet(
            ctx, server.url("/download").toString(), OkHttpClient(), null, "b1/weight.bin", null)
        assertTrue(msg, ok)
        assertFalse(File(ctx.filesDir, "model/meta.json").exists())
    }

    @Test fun applyModelLink_relativeLink_downloadsIntoTrainerLocation() = runBlocking {
        val (ok, msg) = ModelUpdateUtil.applyModelLink(ctx, edgeBase(), "b1/global_model_mobile.pt", OkHttpClient(), null)
        assertTrue(msg, ok)
        assertArrayEquals(res("golden/format/weight.bin").readBytes(), LocalTrainer.downloadedModelFile(ctx.filesDir).readBytes())
    }

    @Test fun applyModelLink_absoluteLink_isUsedAsIs() = runBlocking {
        val (ok, msg) = ModelUpdateUtil.applyModelLink(ctx, "http://unused:1", server.url("/download?rel_path=b1/global_model_mobile.pt").toString(), OkHttpClient(), null)
        assertTrue(msg, ok)
        assertTrue(LocalTrainer.downloadedModelFile(ctx.filesDir).isFile)
    }

    @Test fun applyModelLink_waitsForConcurrentDownload_insteadOfFailingWithLock() = runBlocking {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val rel = request.requestUrl?.queryParameter("rel_path") ?: return MockResponse().setResponseCode(404)
                return when (rel) {
                    "b1/weight.bin" -> MockResponse().setBody(Buffer().write(res("golden/format/weight.bin").readBytes()))
                        .setBodyDelay(800, java.util.concurrent.TimeUnit.MILLISECONDS)
                    "b1/meta.json" -> MockResponse().setBody(res("golden/format/meta.json").readText())
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
        val first = async(Dispatchers.IO) {
            ModelUpdateUtil.applyModelLink(ctx, edgeBase(), "b1/global_model_mobile.pt", OkHttpClient(), null)
        }
        kotlinx.coroutines.delay(200)
        // 対照: 再試行しないとロック中は "lock" で諦める（＝新モデルを取り込まずに次サイクルへ進む原因）
        val noRetry = ModelUpdateUtil.applyModelLink(ctx, edgeBase(), "b1/global_model_mobile.pt", OkHttpClient(), null, lockRetries = 0)
        assertEquals("lock", noRetry.second)
        val second = ModelUpdateUtil.applyModelLink(ctx, edgeBase(), "b1/global_model_mobile.pt", OkHttpClient(), null)
        assertTrue(first.await().second, first.await().first)
        assertTrue("second call must not give up on lock: ${second.second}", second.first)
    }
}
