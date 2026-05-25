package com.example.hfl_experiment.experiment

import android.content.Context
import com.google.gson.Gson
import java.io.File

/**
 * Config loader handling ap_config.json and SharedPreferences fallback.
 */
object Config {
    data class ApConfigData(val id: String, val ssid: String, val psk: String, val priority: Int)
    data class ConfigData(
        val apA: ApConfigData,
        val apB: ApConfigData,
        val modelFile: String,
        val emaAlpha: Float,
        val consecutiveThreshold: Int,
        val minSwitchSeconds: Long,
        val deltaThreshold: Float,
        val cycleWindow: Int,
        val needTP: Map<String, Double>,
        val needRTT: Map<String, Int>
    ) {
        val apNum: Int = 2
        fun getApIdByIndex(idx: Int): String = if (idx == 0) apA.id else apB.id
        fun getLastSelectedIndexSafe(context: Context): Int? {
            return try {
                val prefs = context.getSharedPreferences("network_prefs", Context.MODE_PRIVATE)
                if (prefs.contains("last_selected_index")) {
                    val v = prefs.getInt("last_selected_index", -1)
                    if (v >= 0) v else null
                } else null
            } catch (_: Exception) { null }
        }
    }

    private val gson = Gson()

    fun loadConfigFromAssets(context: Context, assetPath: String = "ap_config.json"): ConfigData {
        val txt = try { context.assets.open(assetPath).bufferedReader().readText() } catch (_: Exception) { "" }
        if (txt.isBlank()) throw IllegalStateException("ap_config.json not found in assets")
        return gson.fromJson(txt, ConfigData::class.java)
    }

    fun loadConfigFromFile(file: File): ConfigData {
        val txt = file.readText()
        return gson.fromJson(txt, ConfigData::class.java)
    }

    fun swapAandB(config: ConfigData): ConfigData {
        return ConfigData(
            apA = config.apB,
            apB = config.apA,
            modelFile = config.modelFile,
            emaAlpha = config.emaAlpha,
            consecutiveThreshold = config.consecutiveThreshold,
            minSwitchSeconds = config.minSwitchSeconds,
            deltaThreshold = config.deltaThreshold,
            cycleWindow = config.cycleWindow,
            needTP = config.needTP,
            needRTT = config.needRTT
        )
    }
}
