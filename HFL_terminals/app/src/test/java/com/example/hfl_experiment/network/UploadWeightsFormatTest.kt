package com.example.hfl_experiment.network

import com.example.hfl_experiment.training.core.Golden
import org.junit.Assert.assertArrayEquals
import org.junit.Test
import java.io.ByteArrayOutputStream

class UploadWeightsFormatTest {
    @Test fun uploadBytes_matchServerSpecOrder() {
        val p = Golden.toParams(Golden.named(Golden.json("golden/format/params.json")), 6, 8, 4)
        val out = ByteArrayOutputStream()
        writeModelParamsLE(p, out)
        val expected = javaClass.classLoader!!.getResource("golden/format/upload_expected.bin")!!.readBytes()
        assertArrayEquals(expected, out.toByteArray())
    }
}
