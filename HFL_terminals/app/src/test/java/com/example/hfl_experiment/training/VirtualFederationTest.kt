package com.example.hfl_experiment.training

import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.network.writeModelParamsLE
import com.example.hfl_experiment.training.core.LocalTrainer
import com.example.hfl_experiment.training.core.ModelParameters
import com.example.hfl_experiment.training.data.DataSample
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.annotation.Config
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.random.Random

/**
 * 仮想端末による連合学習ループの統合テスト（SP1 の受け入れ確認・実機確認 5.2 の PC 版）。
 *
 * 端末側は実コード（LocalTrainer の自動読込・chooseTrainingInit・usedDownloadedSha・writeModelParamsLE）を使い、
 * 中央はアップロードバイト列をサーバ復元仕様の順で読み FedAvg し、変換スクリプトと同じ固定順（キー名ソート）で
 * 各端末の filesDir/model/weight.bin に配る。修正前の挙動（毎サイクルランダム初期値）と比較する。
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class VirtualFederationTest {
    @get:Rule val tmp = TemporaryFolder()

    private val inDim = 6
    private val hid = 16
    private val outDim = 4
    private val terminals = 4
    private val rounds = 6

    private val serverOrder = listOf(
        "layer1.weight", "layer1.bias", "norm1.weight", "norm1.bias",
        "layer2.weight", "layer2.bias", "norm2.weight", "norm2.bias",
        "layer3.weight", "layer3.bias",
    )

    /** 学習可能な合成タスク: 固定の線形規則でラベルを決める（端末ごとに別サンプル）。 */
    private val rule = Random(42).let { r -> List(outDim) { FloatArray(inDim) { r.nextFloat() * 2f - 1f } } }
    private fun label(x: FloatArray) = rule.indices.maxByOrNull { k -> rule[k].indices.sumOf { (rule[k][it] * x[it]).toDouble() } }!!
    private fun samples(seed: Int, n: Int) = Random(seed).let { r ->
        List(n) { FloatArray(inDim) { r.nextFloat() * 2f - 1f }.let { x -> DataSample(x, label(x)) } }
    }
    private val localData = List(terminals) { samples(100 + it, 120) }
    private val evalData = samples(999, 400)

    private fun trainer(data: List<DataSample>, dir: File?, init: ModelParameters?) = LocalTrainer(
        inputSize = inDim, hiddenSize = hid, outputSize = outDim, dropoutP = 0f, learningRate = 0.01f,
        weightDecay = 0.0f, localEpochs = 2, localBatchSize = 16, clipMaxNorm = 1f, warmupSteps = 1,
        totalSteps = 100, minLrRatio = 0.1f, logger = {}, fullDataset = data,
        userIndices = data.indices.toList(), appFilesDir = dir ?: tmp.newFolder(), initialParams = init,
    )

    private fun accuracy(p: ModelParameters): Double {
        val logits = trainer(evalData, null, p).forwardForTest(evalData.map { it.features })
        return evalData.indices.count { i -> logits[i].indices.maxByOrNull { logits[i][it] } == evalData[i].label }.toDouble() / evalData.size
    }

    // ---- 中央（サーバ相当）: アップロードバイト列 → 名前付きテンソル → FedAvg → 配布 ----
    private fun sizes() = linkedMapOf(
        "layer1.weight" to hid * inDim, "layer1.bias" to hid, "norm1.weight" to hid, "norm1.bias" to hid,
        "layer2.weight" to hid * hid, "layer2.bias" to hid, "norm2.weight" to hid, "norm2.bias" to hid,
        "layer3.weight" to outDim * hid, "layer3.bias" to outDim,
    )

    private fun parseUpload(bytes: ByteArray): Map<String, FloatArray> {
        val fb = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asFloatBuffer()
        return serverOrder.associateWith { k -> FloatArray(sizes().getValue(k)).also { fb.get(it) } }
    }

    private fun upload(p: ModelParameters): ByteArray = ByteArrayOutputStream().also { writeModelParamsLE(p, it) }.toByteArray()

    private fun fedAvg(models: List<Map<String, FloatArray>>, weights: List<Int>): Map<String, FloatArray> {
        val total = weights.sum().toFloat()
        return serverOrder.associateWith { k ->
            FloatArray(sizes().getValue(k)) { i -> models.indices.sumOf { m -> (models[m].getValue(k)[i] * weights[m] / total).toDouble() }.toFloat() }
        }
    }

    /** 変換スクリプト(pt_to_meta_weights.py)と同じ: キー名ソート順で weight.bin を書く（meta 無し）。 */
    private fun distribute(global: Map<String, FloatArray>, filesDir: File) {
        val f = LocalTrainer.downloadedModelFile(filesDir).also { it.parentFile!!.mkdirs() }
        val bb = ByteBuffer.allocate(global.values.sumOf { it.size } * 4).order(ByteOrder.LITTLE_ENDIAN)
        global.keys.sorted().forEach { k -> global.getValue(k).forEach { bb.putFloat(it) } }
        f.writeBytes(bb.array())
    }

    private fun toParams(t: Map<String, FloatArray>): ModelParameters {
        fun rows(a: FloatArray, r: Int, c: Int) = List(r) { x -> List(c) { y -> a[x * c + y] } }
        return ModelParameters(
            rows(t.getValue("layer1.weight"), hid, inDim), t.getValue("layer1.bias").toList(),
            t.getValue("norm1.weight").toList(), t.getValue("norm1.bias").toList(),
            rows(t.getValue("layer2.weight"), hid, hid), t.getValue("layer2.bias").toList(),
            t.getValue("norm2.weight").toList(), t.getValue("norm2.bias").toList(),
            rows(t.getValue("layer3.weight"), outDim, hid), t.getValue("layer3.bias").toList(),
        )
    }

    /** 端末の状態（TrainingViewModel の SP1 フィールドと同じもの）。 */
    private class Terminal(val filesDir: File) {
        var lastTrained: ModelParameters? = null
        var lastUsedSha: String? = null
        val sources = mutableListOf<String>()
    }

    private fun runCycle(t: Terminal, data: List<DataSample>): ModelParameters = runBlocking {
        val f = LocalTrainer.downloadedModelFile(t.filesDir)
        val sha = if (f.isFile) WeightBinLoader.sha256Hex(f) else null
        val tr = trainer(data, t.filesDir, chooseTrainingInit(sha, t.lastUsedSha, t.lastTrained))
        t.lastUsedSha = usedDownloadedSha(tr.initSource, sha, t.lastUsedSha)
        t.sources += tr.initSource
        tr.updateWeightsAndGetLoss({ _, _, _ -> })
        tr.getModelParameters().also { t.lastTrained = it }
    }

    /** 修正後: 毎ラウンド、配布されたグローバルから学習。ラウンド間に「新モデル無し」の追加サイクルも挟む。 */
    private fun federated(): Pair<List<Double>, List<Terminal>> {
        val ts = List(terminals) { Terminal(tmp.newFolder("t$it")) }
        val accs = mutableListOf<Double>()
        repeat(rounds) { r ->
            val uploads = ts.mapIndexed { i, t ->
                if (r == 1) runCycle(t, localData[i])   // 新しいグローバルが無いサイクル → 自モデル継続
                parseUpload(upload(runCycle(t, localData[i])))
            }
            val global = fedAvg(uploads, localData.map { it.size })
            ts.forEach { distribute(global, it.filesDir) }
            accs += accuracy(toParams(global))
        }
        return accs to ts
    }

    /** 修正前の挙動: 毎サイクル、ランダム初期値から学習（グローバルを使わない）。 */
    private fun legacy(): List<Double> = List(rounds) {
        val uploads = localData.map { d ->
            runBlocking { trainer(d, null, null).also { it.updateWeightsAndGetLoss({ _, _, _ -> }) } }.getModelParameters().let { parseUpload(upload(it)) }
        }
        accuracy(toParams(fedAvg(uploads, localData.map { it.size })))
    }

    @Test fun federatedLoop_accumulatesLearning_andBeatsLegacy() {
        val (fed, ts) = federated()
        val old = legacy()
        val report = "fixed=${fed.map { "%.3f".format(it) }} legacy=${old.map { "%.3f".format(it) }} sources=${ts[0].sources}"
        println("VirtualFederation: $report")
        File(System.getProperty("java.io.tmpdir"), "virtual_federation_report.txt").writeText(report)

        // 初期値の出所: 1 回目はランダム、新グローバル到着時は DOWNLOADED、新モデル無しのサイクルは PROVIDED
        assertEquals("RANDOM", ts[0].sources[0])
        assertEquals(listOf("DOWNLOADED", "PROVIDED"), ts[0].sources.subList(1, 3))
        assertTrue(report, ts[0].sources.drop(3).all { it == "DOWNLOADED" })
        // 学習が積み上がる: 最終ラウンドは第1ラウンドより明確に高い
        assertTrue(report, fed.last() >= fed.first() + 0.10)
        // 修正前（毎回ランダム初期値）より明確に高い
        assertTrue(report, fed.last() >= old.last() + 0.10)
    }
}
