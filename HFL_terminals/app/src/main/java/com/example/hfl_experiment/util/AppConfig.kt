package com.example.hfl_experiment.util

/**
 * アプリ全体で使う設定値（主にホスト/URL）を一元管理します。
 * 実行時に書き換え可能な var を用意しているので、テストや設定画面からの切替が可能です。
 */
object AppConfig {
    // デフォルトのホスト（スキーム付き）。端末→エッジの既定と揃える（ポートはエッジに合わせる）。
    // ここだけ書き換えればこのユーティリティ経由の接続先を変更できます。
    @Volatile
    var BASE_HOST: String = "http://192.168.11.6:8001"

    // エンドポイントパス（必要に応じて変更）
    var UPLOAD_PATH: String = "/upload"
    var DOWNLOAD_PATH: String = "/download"
    var PING_PATH: String = "/ping"
    var RESOURCE_PATH: String = "/resource"

    // 完全な URL を返すプロパティ
    val UPLOAD_URL: String get() = BASE_HOST + UPLOAD_PATH
    val DOWNLOAD_URL: String get() = BASE_HOST + DOWNLOAD_PATH
    val PING_URL: String get() = BASE_HOST + PING_PATH
    val RESOURCE_URL: String get() = BASE_HOST + RESOURCE_PATH

    // ランタイムでベースホストを変更するヘルパー
    fun setBaseHost(hostWithScheme: String) {
        // 簡易な正規化: 空なら無視
        if (hostWithScheme.isNotBlank()) {
            BASE_HOST = hostWithScheme
        }
    }
}

