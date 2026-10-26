package com.example.hfl_experiment.util.networking

import org.junit.Assert.assertEquals
import org.junit.Test

/** fix-audit-20261015 E7: スループットは Mbps（10^6 bit/s）。以前は MB/s（bytes / 2^20 / 秒）を返していた。 */
class NetworkBandwidthProbeTest {
    @Test fun oneMebibyteInOneSecond() = assertEquals(8.388608, NetworkBandwidthProbe.toMbps(1_048_576L, 1000L), 1e-9)

    @Test fun fortyMbps() = assertEquals(40.0, NetworkBandwidthProbe.toMbps(5_000_000L, 1000L), 1e-9)

    @Test fun zeroMsIsClampedToOne() = assertEquals(8.0, NetworkBandwidthProbe.toMbps(1_000L, 0L), 1e-9)

    @Test fun unitLabelIsMbps() = assertEquals("Mbps", NetworkBandwidthProbe.TP_UNIT)
}
