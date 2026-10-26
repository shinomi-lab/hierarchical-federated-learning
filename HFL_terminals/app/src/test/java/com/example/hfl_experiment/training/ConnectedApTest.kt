package com.example.hfl_experiment.training

import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/** record-connected-ap 2.1: 実際に繋がった SSID と AP 番号。取れないときは null（AP0 に置き換えない）。 */
class ConnectedApTest {
    private val a = "HFL_A24"
    private val b = "HFL_B"

    @Test fun primaryIsAp0() = assertEquals(0, apIndexOfSsid("HFL_A24", a, b))
    @Test fun secondaryIsAp1() = assertEquals(1, apIndexOfSsid("HFL_B", a, b))
    @Test fun otherSsidIsNull() = assertNull(apIndexOfSsid("cafe-wifi", a, b))
    @Test fun nullSsidIsNull() = assertNull(apIndexOfSsid(null, a, b))

    @Test fun quotesAreStripped() = assertEquals("HFL_B", normalizeSsid("\"HFL_B\""))

    @Test fun unknownSsidMarkersAreNull() {
        for (raw in listOf("<unknown ssid>", "\"<unknown ssid>\"", "", "  ", "0x", "\"0x\"", null)) {
            assertNull("raw=$raw", normalizeSsid(raw))
        }
    }

    @Test fun resolveKnownAndUnknown() {
        assertEquals(ConnectedAp("HFL_B", 1, "ssid"), resolveConnectedAp("\"HFL_B\"", a, b))
        assertEquals(ConnectedAp("cafe-wifi", null), resolveConnectedAp("cafe-wifi", a, b))
        assertEquals(ConnectedAp(null, null), resolveConnectedAp("<unknown ssid>", a, b))
    }

    @Test fun routerIdIsRealSsidOrUnknown() {
        assertEquals("HFL_B", routerIdOf(ConnectedAp("HFL_B", 1)))
        assertEquals("unknown", routerIdOf(ConnectedAp(null, null)))
    }

    @Test fun routerIdNeverMatchesServerDelayMap() {
        // サーバの仮想ルータ遅延の対応表のキー。一致すると意図しない遅延が掛かる
        val serverKeys = setOf("RouterA-A", "RouterB")
        for (c in listOf(ConnectedAp("HFL_A24", 0), ConnectedAp("HFL_B", 1), ConnectedAp(null, null))) {
            assertFalse(routerIdOf(c) in serverKeys)
        }
    }

    // --- ap-detection-fallback: SSID が取れないとき Wi‑Fi の IP のサブネットで補う ---
    @Test fun apIndexOfIpMatchesPrefix() {
        assertEquals(0, apIndexOfIp("192.168.11.14", "192.168.11.", "192.168.12."))
        assertEquals(1, apIndexOfIp("192.168.12.3", "192.168.11.", "192.168.12."))
        assertNull(apIndexOfIp("10.0.0.5", "192.168.11.", "192.168.12."))
        assertNull(apIndexOfIp(null, "192.168.11.", "192.168.12."))
        assertNull(apIndexOfIp("", "192.168.11.", "192.168.12."))
        // 先頭一致は「区切りまで」: 192.168.110.x を 192.168.11. と誤認しない
        assertNull(apIndexOfIp("192.168.110.4", "192.168.11.", "192.168.12."))
    }

    @Test fun unknownSsidFallsBackToSubnet() {
        assertEquals(ConnectedAp(null, 1, "subnet"), resolveConnectedAp("<unknown ssid>", a, b, ip = "192.168.12.7"))
        assertEquals(ConnectedAp(null, 0, "subnet"), resolveConnectedAp(null, a, b, ip = "192.168.11.13"))
    }

    @Test fun ssidWinsOverSubnet() {
        // SSID が取れて設定と一致するなら IP は見ない（IP が食い違っていても SSID）
        assertEquals(ConnectedAp("HFL_A24", 0, "ssid"), resolveConnectedAp("HFL_A24", a, b, ip = "192.168.12.7"))
    }

    @Test fun knownButUnconfiguredSsidIsNotCompletedBySubnet() {
        assertEquals(ConnectedAp("cafe-wifi", null, null), resolveConnectedAp("cafe-wifi", a, b, ip = "192.168.12.7"))
    }

    @Test fun neitherSsidNorIpGivesNull() {
        assertEquals(ConnectedAp(null, null, null), resolveConnectedAp("<unknown ssid>", a, b, ip = null))
        assertEquals(ConnectedAp(null, null, null), resolveConnectedAp("<unknown ssid>", a, b, ip = "10.0.0.5"))
    }

    @Test fun routerIdForSubnetDetectedApIsTheConfiguredSsid() {
        assertEquals("HFL_B", routerIdOf(ConnectedAp(null, 1, "subnet"), a, b))
        assertEquals("HFL_A24", routerIdOf(ConnectedAp(null, 0, "subnet"), a, b))
        assertEquals("unknown", routerIdOf(ConnectedAp(null, null, null), a, b))
        assertEquals("HFL_B", routerIdOf(ConnectedAp("HFL_B", 1, "ssid"), a, b))
    }

    @Test fun afterSwitchStatusByAp() {
        assertEquals("true", afterSwitchStatusByAp(ConnectedAp(null, 1, "subnet"), 1, timedOut = false))
        assertEquals("true", afterSwitchStatusByAp(ConnectedAp("x", null, null), null, timedOut = false))   // 提案なし
        assertEquals("unknown", afterSwitchStatusByAp(ConnectedAp(null, null, null), 1, timedOut = true))  // 判定できない
        assertEquals("false", afterSwitchStatusByAp(ConnectedAp("HFL_A24", 0, "ssid"), 1, timedOut = true))
        assertEquals("unknown", afterSwitchStatusByAp(ConnectedAp("HFL_A24", 0, "ssid"), 1, timedOut = false))
    }

    @Test fun apChangedDuringAfter() {
        assertTrue(apChanged(ConnectedAp("HFL_A24", 0), ConnectedAp("HFL_B", 1)))
        assertFalse(apChanged(ConnectedAp("HFL_B", 1), ConnectedAp("HFL_B", 1)))
        // 片方が取れていないときは「変わった」と断定しない
        assertFalse(apChanged(ConnectedAp(null, null), ConnectedAp("HFL_B", 1)))
    }
}

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class ConnectedApMetricsTest {
    @Test fun rowHasSsidAndApOrNulls() {
        val row = putConnectedAp(org.json.JSONObject(), ConnectedAp("HFL_B", 1, "ssid"))
        assertEquals("HFL_B", row.getString("connected_ssid"))
        assertEquals(1, row.getInt("connected_ap"))
        assertEquals("ssid", row.getString("connected_ap_source"))
        val sub = putConnectedAp(org.json.JSONObject(), ConnectedAp(null, 1, "subnet"))
        assertEquals("subnet", sub.getString("connected_ap_source"))
        assertTrue(sub.isNull("connected_ssid"))
        val none = putConnectedAp(org.json.JSONObject(), ConnectedAp(null, null))
        assertTrue(none.has("connected_ssid") && none.isNull("connected_ssid"))
        assertTrue(none.has("connected_ap") && none.isNull("connected_ap"))
    }

    @Test fun afterRowHasStartEndAndChangedFlag() {
        val row = putAfterAp(org.json.JSONObject(), ConnectedAp("HFL_A24", 0), ConnectedAp("HFL_B", 1))
        assertEquals(0, row.getInt("connected_ap_after_start"))
        assertEquals(1, row.getInt("connected_ap_after_end"))
        assertEquals("HFL_B", row.getString("connected_ssid_after_end"))
        assertTrue(row.getBoolean("ap_changed_during_after"))
        val same = putAfterAp(org.json.JSONObject(), ConnectedAp("HFL_B", 1), ConnectedAp("HFL_B", 1))
        assertFalse(same.getBoolean("ap_changed_during_after"))
    }

    @Test fun nullAfterApStaysNull() {
        val row = putAfterAp(org.json.JSONObject(), ConnectedAp(null, null), ConnectedAp(null, null))
        assertTrue(row.isNull("connected_ap_after_start") && row.isNull("connected_ap_after_end"))
        assertFalse(row.getBoolean("ap_changed_during_after"))
    }
}
