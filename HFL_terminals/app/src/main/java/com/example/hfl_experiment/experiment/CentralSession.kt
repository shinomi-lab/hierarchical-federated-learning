package com.example.hfl_experiment.experiment

import org.json.JSONArray
import org.json.JSONObject

/**
 * 問い合わせ系モード（central・hfl_rec）のセッションの判断。TrainingViewModel の分岐をここに出して JVM テストで固定する。
 * central（centralized-mode）は提案もしない対照群。hfl_rec（hfl-recommended-ap-mode）は同じ流れで、推奨 AP への切替を提案する。
 */
object CentralSessionPolicy {
    const val MODE = "central"
    const val HFL_REC_MODE = "hfl_rec"

    /** 学習せず中央へ問い合わせるモードか。 */
    fun isQueryMode(mode: String): Boolean = mode == MODE || mode == HFL_REC_MODE

    /** 問い合わせ先のパス。central は中央モデル、hfl_rec は最新のグローバルモデル。 */
    fun queryPath(mode: String): String = if (mode == HFL_REC_MODE) CentralClient.HFL_REC_PATH else CentralClient.PATH

    /** 中央への問い合わせはサイクルごとに 1 回（ベースラインの計測のうち、各サイクルの 1 回目）。 */
    fun queryDue(mode: String, epochInCycle: Int): Boolean = isQueryMode(mode) && epochInCycle == 1

    /** central は推奨 AP を自動では適用せず、提案もしない（AP の切替は実験者が手で行う。決定 E1）。hfl_rec と他のベースラインは提案する。 */
    fun shouldProposeApSwitch(mode: String): Boolean = mode != MODE

    /** 推奨 AP と今繋がっている AP が違う（または繋がっている AP が不明）なら、切替を提案する。 */
    fun needsSwitch(recommended: Int, connected: Int?): Boolean = connected != recommended

    /** 問い合わせ系は学習も重みの送信もしない。セッション末尾の自動アップロード呼び出しを行わない。他のベースラインは従来どおり。 */
    fun shouldUploadAtEnd(mode: String): Boolean = !isQueryMode(mode)
}

/** 1 サイクルの問い合わせ結果。 */
data class CentralCycleRecord(val cycle: Int, val outcome: CentralOutcome)

/**
 * セッション要約行（baseline_session）に足す central_* 項目。
 * central_status は全て成功なら "ok"、失敗があれば最初の失敗の種類（問い合わせが 0 回なら "no_query"）。
 * central_rtt_ms は成功した問い合わせの往復時間の平均、central_ap・central_model_sha256 は最後に成功した応答の値（無ければ null）。
 * サイクルごとの詳細は central_queries。
 */
fun centralSessionFields(records: List<CentralCycleRecord>, url: String): JSONObject {
    val oks = records.filter { it.outcome.status == CentralClient.STATUS_OK }
    val firstFailure = records.firstOrNull { it.outcome.status != CentralClient.STATUS_OK }
    val lastOk = oks.lastOrNull()?.outcome
    return JSONObject()
        .put("central_status", when {
            records.isEmpty() -> "no_query"
            firstFailure != null -> firstFailure.outcome.status
            else -> CentralClient.STATUS_OK
        })
        .put("central_rtt_ms", if (oks.isEmpty()) JSONObject.NULL else oks.map { it.outcome.rttMs }.average())
        .put("central_ap", lastOk?.chosenAp ?: JSONObject.NULL)
        .put("central_model_sha256", lastOk?.modelSha256 ?: JSONObject.NULL)
        .put("central_url", url)
        .put("central_queries_n", records.size)
        .put("central_ok_n", oks.size)
        .put("central_queries", JSONArray().apply {
            records.forEach { r ->
                put(JSONObject()
                    .put("cycle", r.cycle)
                    .put("status", r.outcome.status)
                    .put("rtt_ms", r.outcome.rttMs)
                    .put("ap", r.outcome.chosenAp ?: JSONObject.NULL)
                    .put("server_ms", r.outcome.serverMs ?: JSONObject.NULL)
                    .put("http_code", r.outcome.httpCode ?: JSONObject.NULL))
            }
        })
}


/**
 * hfl_rec の要約行に足す hfl_rec_* 項目（central_* と同じ集計。接頭辞だけ別）。
 * hfl_rec_round・hfl_rec_is_initial は最後に成功した応答の値。followed は After の終了後に [hflRecFollowed] で足す。
 */
fun hflRecSessionFields(records: List<CentralCycleRecord>, url: String): JSONObject {
    val oks = records.filter { it.outcome.status == CentralClient.STATUS_OK }
    val firstFailure = records.firstOrNull { it.outcome.status != CentralClient.STATUS_OK }
    val lastOk = oks.lastOrNull()?.outcome
    return JSONObject()
        .put("hfl_rec_status", when {
            records.isEmpty() -> "no_query"
            firstFailure != null -> firstFailure.outcome.status
            else -> CentralClient.STATUS_OK
        })
        .put("hfl_rec_rtt_ms", if (oks.isEmpty()) JSONObject.NULL else oks.map { it.outcome.rttMs }.average())
        .put("hfl_rec_ap", lastOk?.chosenAp ?: JSONObject.NULL)
        .put("hfl_rec_model_sha256", lastOk?.modelSha256 ?: JSONObject.NULL)
        .put("hfl_rec_round", lastOk?.round ?: JSONObject.NULL)
        .put("hfl_rec_is_initial", lastOk?.isInitial ?: JSONObject.NULL)
        .put("hfl_rec_url", url)
        .put("hfl_rec_queries_n", records.size)
        .put("hfl_rec_ok_n", oks.size)
        .put("hfl_rec_queries", JSONArray().apply {
            records.forEach { r ->
                put(JSONObject()
                    .put("cycle", r.cycle)
                    .put("status", r.outcome.status)
                    .put("rtt_ms", r.outcome.rttMs)
                    .put("ap", r.outcome.chosenAp ?: JSONObject.NULL)
                    .put("server_ms", r.outcome.serverMs ?: JSONObject.NULL)
                    .put("http_code", r.outcome.httpCode ?: JSONObject.NULL))
            }
        })
}

/**
 * 推奨に従ったか。推奨 AP と、After の計測終了時に繋がっていた AP の一致で決める。
 * 問い合わせが全て成功でない・推奨がない・繋がった AP が不明なら null（「従わなかった」にはしない）。
 */
fun hflRecFollowed(status: String, recommendedAp: Int?, connectedApAfterEnd: Int?): Boolean? =
    if (status != CentralClient.STATUS_OK || recommendedAp == null || connectedApAfterEnd == null) null
    else recommendedAp == connectedApAfterEnd
