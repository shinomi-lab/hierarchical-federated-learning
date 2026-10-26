package com.example.hfl_experiment.util  // 適宜変更

import com.example.hfl_experiment.training.data.DataSample
import java.io.File
import java.io.IOException

object CsvLoader {

    /**
     * CSVファイルから DataSample のリストを読み込む
     * 最後の列がラベル（Int）、それ以前が特徴量（Float）である前提
     */
    @Throws(IOException::class, NumberFormatException::class)
    fun loadFromFile(file: File): List<DataSample> {
        if (!file.exists()) throw IOException("CSVファイルが存在しません: ${file.path}")

        return file.readLines()
            .filter { it.isNotBlank() }  // 空行を除外
            .mapIndexed { index, line ->
                val parts = line.trim().split(",")
                if (parts.size < 2) throw IOException("CSVの形式が不正です（行 $index）: $line")

                val features = parts.dropLast(1).map {
                    it.toFloatOrNull() ?: throw NumberFormatException("Floatに変換できません: '$it'")
                }.toFloatArray()

                val label = parts.last().toIntOrNull()
                    ?: throw NumberFormatException("ラベルがIntに変換できません: '${parts.last()}'")

                DataSample(features, label)
            }
    }
}
