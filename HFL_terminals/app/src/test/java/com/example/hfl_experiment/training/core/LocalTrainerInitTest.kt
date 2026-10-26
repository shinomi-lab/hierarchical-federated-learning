package com.example.hfl_experiment.training.core

import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.training.data.DataSample
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.annotation.Config
import java.io.File
import kotlin.random.Random

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class LocalTrainerInitTest {
    @get:Rule val tmp = TemporaryFolder()
    private fun res(p: String) = File(javaClass.classLoader!!.getResource(p)!!.toURI())
    private val known = Golden.toParams(Golden.named(Golden.json("golden/format/params.json")), 6, 8, 4)
    private val expect = Golden.json("golden/format/expect.json")
    private val probe = Golden.rows(expect.getAsJsonArray("inputs"))

    private fun trainer(dir: File, init: ModelParameters? = null): LocalTrainer {
        val rnd = Random(1)
        val d = List(64) { i -> DataSample(FloatArray(6) { rnd.nextFloat() }, i % 4) }
        return LocalTrainer(
            inputSize = 6, hiddenSize = 8, outputSize = 4, dropoutP = 0f, learningRate = 0.01f,
            weightDecay = 0f, localEpochs = 1, localBatchSize = 16, clipMaxNorm = 1f, warmupSteps = 1,
            totalSteps = 10, minLrRatio = 0.1f, logger = {}, fullDataset = d,
            userIndices = d.indices.toList(), appFilesDir = dir, initialParams = init,
        )
    }

    private fun dirWith(meta: Boolean): File {
        val dir = tmp.newFolder()
        File(dir, "model").mkdirs()
        res("golden/format/weight.bin").copyTo(LocalTrainer.downloadedModelFile(dir))
        if (meta) res("golden/format/meta.json").copyTo(File(dir, "model/meta.json"))
        return dir
    }

    private fun assertMatchesPyTorch(t: LocalTrainer) {
        val expected = Golden.rows(expect.getAsJsonArray("logits"))
        val actual = t.forwardForTest(probe)
        expected.indices.forEach { r -> Golden.assertClose("logits row $r", expected[r], actual[r]) }
    }

    @Test fun downloadedModelWithMeta_isLoadedByName() {
        val t = trainer(dirWith(meta = true))
        assertEquals("DOWNLOADED", t.initSource)
        assertMatchesPyTorch(t)
    }

    @Test fun downloadedModelWithoutMeta_isLoadedInConverterOrder() {
        val t = trainer(dirWith(meta = false))
        assertEquals("DOWNLOADED", t.initSource)
        assertMatchesPyTorch(t)
    }

    @Test fun providedParams_winOverDownloaded() {
        val t = trainer(dirWith(meta = true), init = known.copy(biases3 = listOf(9f, 9f, 9f, 9f)))
        assertEquals("PROVIDED", t.initSource)
        assertEquals(listOf(9f, 9f, 9f, 9f), t.getModelParameters().biases3)
    }

    @Test fun wrongSizedDownload_isRecordedAsLoadFailure() {
        val dir = tmp.newFolder()
        File(dir, "model").mkdirs()
        LocalTrainer.downloadedModelFile(dir).writeBytes(ByteArray(12))
        assertEquals("RANDOM_LOAD_FAILED", trainer(dir).initSource)
    }

    @Test fun nothing_isRandom() = assertEquals("RANDOM", trainer(tmp.newFolder()).initSource)

    @Test fun trainingFromLoadedModel_changesWeights() = runBlocking {
        val t = trainer(dirWith(meta = true))
        val before = Golden.flat(t.getModelParameters()).values.flatMap { it.toList() }
        t.updateWeightsAndGetLoss({ _, _, _ -> })
        val after = Golden.flat(t.getModelParameters()).values.flatMap { it.toList() }
        assertFalse("読込後に学習しても重みが変わらない（optimizer 参照切れ）", before == after)
    }

    @Test fun providedParams_areCopiedExactly() {
        val t = trainer(tmp.newFolder(), init = known)
        val exp = Golden.flat(known); val act = Golden.flat(t.getModelParameters())
        for (k in exp.keys) assertArrayEquals(k, exp.getValue(k), act.getValue(k), 0f)
    }
}
