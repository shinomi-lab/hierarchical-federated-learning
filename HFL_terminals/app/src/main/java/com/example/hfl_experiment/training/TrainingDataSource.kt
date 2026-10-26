package com.example.hfl_experiment.training

import java.io.File
import java.io.InputStream
import java.security.MessageDigest

/**
 * rebuild-training-data-20261022: 端末が学習に使う教師データの選び方。
 * 端末の files/training_data.csv（上書き）があればそれ、無ければ APK のアセット training_data.csv。
 * 条件（対称・非対称）の切り替えで APK を作り直さないための口。上書きがあるのに読めないときは、呼び出し側がアセットに
 * 切り替えずにエラーにする（黙った代用は条件の取り違えを見えなくする）。
 */
const val TRAINING_DATA_OVERRIDE_NAME = "training_data.csv"

data class TrainingDataChoice(val source: String, val file: File, val sha256: String)

/** メトリクスの行に入れる、実際に読んだ教師データの出所・SHA256・行数。 */
data class TrainingDataInfo(val source: String, val sha256: String, val rows: Int)

fun sha256Hex(file: File): String {
    val md = MessageDigest.getInstance("SHA-256")
    file.inputStream().use { ins ->
        val buf = ByteArray(64 * 1024)
        while (true) { val n = ins.read(buf); if (n < 0) break; md.update(buf, 0, n) }
    }
    return md.digest().joinToString("") { "%02x".format(it) }
}

/**
 * 上書きファイルがあれば "override"、無ければアセットを assetCopy に写して "asset"。どちらも無ければ null。
 * 上書きファイルが空（0 バイト）でも "override" として返す（読めない扱いは呼び出し側）。
 */
fun chooseTrainingCsv(overrideFile: File, openAsset: () -> InputStream?, assetCopy: File): TrainingDataChoice? {
    if (overrideFile.isFile) return TrainingDataChoice("override", overrideFile, sha256Hex(overrideFile))
    val ins = openAsset() ?: return null
    ins.use { src -> assetCopy.outputStream().use { out -> src.copyTo(out) } }
    return TrainingDataChoice("asset", assetCopy, sha256Hex(assetCopy))
}

/** メトリクスの行に、使った教師データを入れる。 */
fun putTrainingData(row: org.json.JSONObject, t: TrainingDataInfo?): org.json.JSONObject {
    if (t == null) return row
    return row.put("training_data_source", t.source).put("training_data_sha256", t.sha256).put("training_data_rows", t.rows)
}
