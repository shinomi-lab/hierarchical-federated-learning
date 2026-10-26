package com.example.hfl_experiment.training

import org.json.JSONObject

/**
 * record-connected-ap: 端末が実際に繋がっている AP の記録。
 * 以前は virtual_router_id が固定の "HFL_A24" で、どの AP に繋がっていたかを一切記録しなかった。
 * 取れない（権限なし・未接続・<unknown ssid>）ときは null にし、AP0 や HFL_A24 に黙って置き換えない。
 */
data class ConnectedAp(val ssid: String?, val ap: Int?, val source: String? = null)

/** WifiManager の SSID（引用符つき、取れないと "<unknown ssid>"・"0x"）を、取れていなければ null にする。 */
fun normalizeSsid(raw: String?): String? {
    val s = raw?.trim()?.trim('"')?.trim() ?: return null
    if (s.isEmpty() || s == "0x" || s.equals("<unknown ssid>", ignoreCase = true)) return null
    return s
}

/** primary の SSID なら 0、secondary なら 1、どちらでもない・取れていないなら null。 */
fun apIndexOfSsid(ssid: String?, primary: String, secondary: String): Int? = when {
    ssid == null -> null
    ssid == primary -> 0
    ssid == secondary -> 1
    else -> null
}

/** Wi‑Fi の IPv4 が prefixA で始まれば 0、prefixB なら 1、どちらでもなければ null（IP は位置情報の権限が要らない）。prefix は "192.168.11." のように区切りの "." まで含める。 */
fun apIndexOfIp(ip: String?, prefixA: String, prefixB: String): Int? = when {
    ip.isNullOrBlank() -> null
    prefixA.isNotEmpty() && ip.startsWith(prefixA) -> 0
    prefixB.isNotEmpty() && ip.startsWith(prefixB) -> 1
    else -> null
}

/**
 * まず SSID を設定と照合する（出所 "ssid"）。SSID が取れない（null / <unknown ssid>）ときだけ、Wi‑Fi の IPv4 のサブネットで補う（出所 "subnet"）。
 * SSID が取れて設定にない（別のネットワーク）ときは補わない。どちらも判定できなければ ap は null。
 */
fun resolveConnectedAp(
    rawSsid: String?, primary: String, secondary: String,
    ip: String? = null, prefixA: String = DEFAULT_SUBNET_A, prefixB: String = DEFAULT_SUBNET_B,
): ConnectedAp {
    val ssid = normalizeSsid(rawSsid)
    if (ssid != null) {
        val ap = apIndexOfSsid(ssid, primary, secondary)
        return ConnectedAp(ssid, ap, if (ap != null) "ssid" else null)
    }
    val ap = apIndexOfIp(ip, prefixA, prefixB)
    return ConnectedAp(null, ap, if (ap != null) "subnet" else null)
}

const val DEFAULT_SUBNET_A = "192.168.11."   // AP0 = HFL_A24 の LAN
const val DEFAULT_SUBNET_B = "192.168.12."   // AP1 = HFL_B（ルータ B の LAN 側）

/** アップロードの virtual_router_id。サーバの仮想ルータ遅延の対応表（RouterA-A・RouterB）に一致しない値だけを返す。 */
fun routerIdOf(c: ConnectedAp, primary: String = "", secondary: String = ""): String = when {
    c.ssid != null -> c.ssid
    c.ap == 0 && primary.isNotEmpty() -> primary      // SSID が隠されていてもサブネットで AP が分かる → 設定のその AP の SSID
    c.ap == 1 && secondary.isNotEmpty() -> secondary
    else -> "unknown"
}

/** 開始と終了で AP が違うか。どちらかが取れていないときは「変わった」と断定しない。 */
fun apChanged(start: ConnectedAp, end: ConnectedAp): Boolean =
    start.ap != null && end.ap != null && start.ap != end.ap

private fun JSONObject.putNullable(key: String, v: Any?): JSONObject = put(key, v ?: JSONObject.NULL)

/** メトリクスの行に、記録した時点の実接続の SSID と AP 番号（取れないときは null）。 */
fun putConnectedAp(row: JSONObject, c: ConnectedAp): JSONObject =
    row.putNullable("connected_ssid", c.ssid).putNullable("connected_ap", c.ap).putNullable("connected_ap_source", c.source)

/** After の開始時と終了時。途中で AP が変わったら ap_changed_during_after を立てる。 */
fun putAfterAp(row: JSONObject, start: ConnectedAp, end: ConnectedAp): JSONObject =
    row.putNullable("connected_ssid_after_start", start.ssid).putNullable("connected_ap_after_start", start.ap)
        .putNullable("connected_ssid_after_end", end.ssid).putNullable("connected_ap_after_end", end.ap)
        .put("ap_changed_during_after", apChanged(start, end))
