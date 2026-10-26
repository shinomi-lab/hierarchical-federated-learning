package com.example.hfl_experiment.training

import org.junit.Assert.assertEquals
import org.junit.Test

/** SP3d: ベースラインの計測回数はサイクル数 × 5（HFL のサイクル×エポックに対応させる）。 */
class BaselineCountTest {
    @Test fun count_isFivePerCycle() {
        assertEquals(25, baselineMeasurementCount(5))
        assertEquals(100, baselineMeasurementCount(20))   // 過去のロングセッション（100 回）と同じ
        assertEquals(15, baselineMeasurementCount(3))
        assertEquals(5, baselineMeasurementCount(1))
    }

    @Test fun cycleEpoch_mapping() {
        assertEquals(1 to 1, baselineCycleEpoch(1)); assertEquals(1 to 5, baselineCycleEpoch(5))
        assertEquals(2 to 1, baselineCycleEpoch(6)); assertEquals(20 to 5, baselineCycleEpoch(100))
    }
}
