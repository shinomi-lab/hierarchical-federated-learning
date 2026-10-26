package com.example.hfl_experiment.work

import androidx.work.ListenableWorker
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class UploadLogsFailureTest {
    @get:Rule val tmp = TemporaryFolder()

    @Test fun clientError_isFailure_filesStay_andReasonRecorded() {
        val logDir = tmp.newFolder("log")
        val f = File(logDir, "a.log").apply { writeText("x") }
        val r = UploadLogsWorker.recordUploadFailure(logDir, 403, "terminal not permitted on this edge")
        assertTrue(r is ListenableWorker.Result.Failure)
        assertTrue(f.exists())
        assertFalse(File(logDir, "sent").exists())
        val st = JSONObject(File(logDir, "upload_status.json").readText())
        assertFalse(st.getBoolean("ok")); assertEquals(403, st.getInt("http_code"))
        assertTrue(st.getString("message").contains("not permitted"))
    }
}
