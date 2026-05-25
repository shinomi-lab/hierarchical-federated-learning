package com.example.hfl_experiment.ui

import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.example.hfl_experiment.R

class ResultActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_result)

        // Get the comparison result from the intent
        val comparisonResult = intent.getStringExtra("comparison_result") ?: "No result available"

        // Display the result
        val resultTextView: TextView = findViewById(R.id.textViewResult)
        resultTextView.text = comparisonResult

        // Set up the close button
        val closeButton: Button = findViewById(R.id.buttonClose)
        closeButton.setOnClickListener {
            finish() // Close the activity and return to the previous screen
        }
    }
}
