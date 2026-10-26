package com.example.hfl_experiment.training

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class TrainingOutputSizeTest {
    @Test fun outputIsApCount_notAppCount() = assertEquals(2, trainingOutputSize(maxLabel = 1, apCount = 2))
    @Test fun labelBeyondApCount_isRejected() {
        assertThrows(IllegalArgumentException::class.java) { trainingOutputSize(maxLabel = 3, apCount = 2) }
    }
    @Test fun defaultApCount_isTwo() = assertEquals(2, trainingOutputSize(maxLabel = 0))
}
