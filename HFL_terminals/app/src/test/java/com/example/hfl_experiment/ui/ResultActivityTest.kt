package com.example.hfl_experiment.ui

import android.content.Intent
import android.widget.TextView
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hfl_experiment.R
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class ResultActivityTest {
    @Test
    fun launches_andShowsComparisonResult() {
        val intent = Intent(ApplicationProvider.getApplicationContext(), ResultActivity::class.java)
            .putExtra("comparison_result", "DTO is consistent with OpenAPI schema.")
        ActivityScenario.launch<ResultActivity>(intent).use { scenario ->
            scenario.onActivity { a ->
                assertEquals("DTO is consistent with OpenAPI schema.", a.findViewById<TextView>(R.id.textViewResult).text.toString())
            }
        }
    }
}
