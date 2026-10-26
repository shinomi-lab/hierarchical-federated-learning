package com.example.hfl_experiment.training.core

import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.training.data.DataSample
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class LocalTrainerGoldenTest {
    @get:Rule val tmp = TemporaryFolder()
    private val g = Golden.json("golden/trainer_golden.json")
    private val cfg = g.getAsJsonObject("config")
    private val i = cfg["input"].asInt
    private val h = cfg["hidden"].asInt
    private val o = cfg["output"].asInt
    private val inputs = Golden.rows(g.getAsJsonArray("inputs"))
    private val targets = g.getAsJsonArray("targets").map { it.asInt }

    private fun trainer(): LocalTrainer {
        val data = inputs.mapIndexed { k, x -> DataSample(x, targets[k]) }
        return LocalTrainer(
            inputSize = i, hiddenSize = h, outputSize = o, dropoutP = 0f,
            learningRate = cfg["lr"].asFloat, weightDecay = cfg["weight_decay"].asFloat,
            localEpochs = 1, localBatchSize = inputs.size, clipMaxNorm = cfg["clip"].asFloat,
            warmupSteps = cfg["warmup"].asInt, totalSteps = cfg["total"].asInt,
            minLrRatio = cfg["min_lr_ratio"].asFloat, logger = {},
            fullDataset = data, userIndices = data.indices.toList(), appFilesDir = tmp.newFolder(),
            initialParams = Golden.toParams(Golden.named(g.getAsJsonObject("init")), i, h, o),
        )
    }

    private fun assertParams(step: String, t: LocalTrainer) {
        val expected = Golden.named(g.getAsJsonObject("after_steps").getAsJsonObject(step))
        val actual = Golden.flat(t.getModelParameters())
        for (k in expected.keys) Golden.assertClose("after $step step: $k", expected.getValue(k), actual.getValue(k))
    }

    @Test fun forward_matchesPyTorch() {
        val expected = Golden.rows(g.getAsJsonArray("logits"))
        val actual = trainer().forwardForTest(inputs)
        expected.indices.forEach { r -> Golden.assertClose("logits row $r", expected[r], actual[r]) }
    }

    @Test fun loss_matchesPyTorch() = assertEquals(g["loss"].asFloat, trainer().lossForTest(inputs, targets), 1e-5f)

    @Test fun gradients_matchPyTorch() {
        val expected = Golden.named(g.getAsJsonObject("grads"))
        val actual = trainer().gradientsForTest(inputs, targets)
        for (k in expected.keys) Golden.assertClose("grad $k", expected.getValue(k), actual.getValue(k))
    }

    @Test fun oneAdamWStep_matchesPyTorch() {
        val t = trainer(); t.trainOnBatch(inputs, targets); assertParams("1", t)
    }

    @Test fun fiveAdamWSteps_matchPyTorch() {
        val t = trainer(); repeat(cfg["steps"].asInt) { t.trainOnBatch(inputs, targets) }; assertParams("5", t)
    }

    @Test fun trainingAfterEvaluation_stillMatchesPyTorch() {
        // I6: エポック末の検証(eval)の直後でも正しい中間値で逆伝播すること
        val t = trainer(); t.forwardForTest(inputs); t.trainOnBatch(inputs, targets); assertParams("1", t)
    }
}
