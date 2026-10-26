package com.example.hfl_experiment.device

import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.example.hfl_experiment.R
import com.example.hfl_experiment.training.data.TrainingLogger
import com.example.hfl_experiment.util.logging.RealTimeLogger
import java.io.File

class DeviceAckTestActivity : AppCompatActivity() {
    private val TAG = "DeviceAckTestActivity"
    private var client: DeviceAckClient? = null
    private var trainingLogger: TrainingLogger? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_device_ack_test)

        val etEdgeHost = findViewById<EditText>(R.id.etEdgeHost)
        val etToken = findViewById<EditText>(R.id.etToken)
        val etDeviceId = findViewById<EditText>(R.id.etDeviceId)
        val btnStart = findViewById<Button>(R.id.btnStart)
        val btnStop = findViewById<Button>(R.id.btnStop)
        val tvStatus = findViewById<TextView>(R.id.tvStatus)

        // prepare TrainingLogger: try filesDir first, fall back to null
        val externalDir = filesDir
        trainingLogger = try {
            TrainingLogger(this, externalDir)
        } catch (e: Exception) {
            RealTimeLogger.w(TAG, "Failed to init TrainingLogger with external dir: ${e.message}. Falling back to filesDir.")
            try {
                TrainingLogger(this, null)
            } catch (ex: Exception) {
                RealTimeLogger.e(TAG, "Failed to init fallback TrainingLogger: ${ex.message}")
                null
            }
        }

        btnStart.setOnClickListener {
            val edgeHost = etEdgeHost.text.toString().trim()
            val token = etToken.text.toString().trim().ifEmpty { null }
            val deviceId = etDeviceId.text.toString().trim().ifEmpty { "device-01" }

            tvStatus.text = "Status: starting..."
            client = DeviceAckClient(
                context = this,
                edgeHost = edgeHost,
                token = token,
                deviceId = deviceId,
                trainingLogger = trainingLogger
            )

            client?.start { modelFile ->
                // Simple apply callback for testing: just log and return true
                RealTimeLogger.i(TAG, "applyCallback invoked with file=${modelFile.absolutePath}")
                // Real implementation should call trainer.loadFromFile(...) and return success/failure
                true
            }
            tvStatus.text = "Status: running (connected to $edgeHost)"
        }

        btnStop.setOnClickListener {
            client?.stop()
            client = null
            tvStatus.text = "Status: stopped"
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        client?.stop()
        client = null
    }
}
