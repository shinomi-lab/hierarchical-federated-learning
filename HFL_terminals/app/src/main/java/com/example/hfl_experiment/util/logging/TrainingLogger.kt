package com.example.hfl_experiment.util.logging

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

class TrainingLogger(private val context: Context) {

    fun saveLoss(epoch: Int, loss: Float) {
        val file = File(context.filesDir, "training_loss_log.json")
        val json = JSONObject().apply {
            put("epoch", epoch)
            put("loss", loss)
        }
        file.appendText(json.toString() + "\n")
    }

    fun saveWeights(epoch: Int, weights: Map<String, FloatArray>) {
        val file = File(context.filesDir, "training_weights_log.json")
        val weightsJson = JSONObject()
        weights.forEach { (key, value) ->
            weightsJson.put(key, JSONArray(value.toList()))
        }

        val json = JSONObject().apply {
            put("epoch", epoch)
            put("weights", weightsJson)
        }
        file.appendText(json.toString() + "\n")
    }
}
