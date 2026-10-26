package com.example.hfl_experiment.training

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.robolectric.annotation.Config

/** rebuild-training-data-20261022 3.1・3.2: 上書き → アセットの順。記録される SHA256 は読んだ CSV のもの。 */
class TrainingDataSourceTest {
    @get:Rule val tmp = TemporaryFolder()

    private val assetBytes = "5.0,0.0,0,0\n".toByteArray()
    private fun asset() = java.io.ByteArrayInputStream(assetBytes)

    @Test fun overrideWinsOverAsset() {
        val ov = tmp.newFile("training_data.csv").apply { writeText("10.0,0.0,1,1\n") }
        var assetOpened = false
        val c = chooseTrainingCsv(ov, { assetOpened = true; asset() }, tmp.newFile("copy.csv"))!!
        assertEquals("override", c.source)
        assertEquals(ov, c.file)
        assertEquals(sha256Hex(ov), c.sha256)
        assertTrue("上書きがあるときはアセットを読まない", !assetOpened)
    }

    @Test fun assetWhenNoOverride() {
        val copy = tmp.newFile("copy.csv")
        val c = chooseTrainingCsv(java.io.File(tmp.root, "training_data.csv"), { asset() }, copy)!!
        assertEquals("asset", c.source)
        assertEquals("5.0,0.0,0,0\n", c.file.readText())
        assertEquals(64, c.sha256.length)
    }

    @Test fun noneWhenNeither() {
        assertNull(chooseTrainingCsv(java.io.File(tmp.root, "training_data.csv"), { null }, tmp.newFile("copy.csv")))
    }

    @Test fun emptyOverrideIsStillOverride() {
        // 空の上書きはアセットに黙って切り替えない（呼び出し側が読めないエラーにする）
        val ov = tmp.newFile("training_data.csv")
        val c = chooseTrainingCsv(ov, { asset() }, tmp.newFile("copy.csv"))!!
        assertEquals("override", c.source)
    }

    @Test fun sha256IsStandard() {
        val f = tmp.newFile("a.csv").apply { writeText("abc") }
        assertEquals("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", sha256Hex(f))
    }
}

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class TrainingDataMetricsTest {
    @Test fun metricsRowHasTrainingData() {
        val row = putTrainingData(org.json.JSONObject(), TrainingDataInfo("override", "ab".repeat(32), 8752))
        assertEquals("override", row.getString("training_data_source"))
        assertEquals(8752, row.getInt("training_data_rows"))
        assertNotNull(row.getString("training_data_sha256"))
        assertTrue(!putTrainingData(org.json.JSONObject(), null).has("training_data_source"))
    }
}
