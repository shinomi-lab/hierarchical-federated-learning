# 端末（クライアント）向け統合ドキュメント

目的
- 端末（TrainingManager / NetworkClient）実装チーム向けに、サーバ側で実装済みの点と、端末側で必ず実装してほしい箇所をまとめる。これをそのままチームへ投げて実装・検証してもらってください。

短い要約（最重要）
- サーバ側は「明示的 ACK を返す」「重複検出（SHAベース）の永続化」「event_timestamp/run_id の透過」を実装済みです。
- 端末側で必須なのは「レスポンス本文の ACK 検査」「タイムアウト延長」「指数バックオフ再試行」「冪等キーの送付（local_seq/run_id/content_sha256）」です。これらがないと端末側の短タイムアウトで無駄な再送が発生します。

1) サーバ側の実装状況（このリポジトリ内で既に適用されている項目）
- 明示的 ACK JSON の返却：実装済み（`receive_terminal_weights` が JSON で `ack`, `ack_timestamp`, `status` 等を返します）。
- event_timestamp / started_at / run_id を端末から受け取り保存・透過：実装済み（edge 側で受け取り、central へ透過するフローが追加済み）。
- central 側での content_sha256 検証と受領ハッシュの永続化（重複検出）：実装済み（`received_edges/received_hashes.json` に永続化）。
- edge 側での sent-mark を「POST 成功後に行う」よう修正：実装済み（送信成功確認後に sent マークを保存）。
- ログの一元化（`logs/time_records` に記録）：実装済み。

注意（未完/要確認）
- 端末→edge→central の経路で `content_sha256` を端末が送った場合、edge が central に転送する際に再計算して添付する仕組みを推奨しています。現状、central の検証は実装済みですが、edge が central へ転送時に確実に `content_sha256` を添付しているかはリポジトリ内の edge の送信コードの確認が必要です（必要であればこちらで patch 提案可能）。
- 古い aggregate/run ディレクトリの GC 方針や central で同ラウンドに複数 run が届いたときの選択ポリシーは運用方針依存で未実装／要決定です。

2) 端末側に必須で実装してほしいこと（そのまま実装チェックリストとして渡してください）
- 必須（高優先）
    - `event_timestamp`, `started_at`, `local_seq`, 可能なら `run_id` を送ること。
    - HTTP レスポンスの本文を JSON として必ず読み、`ack === true` または `status === "duplicate_ignored"` を受け取ったら成功扱いにする。
    - リクエストの call/read/write タイムアウトを短くしない（推奨 60–120s）。
    - 再試行は指数バックオフ + jitter（例を下に記載）。再試行間隔や回数は運用に合わせて調整。
    - 送信するペイロードの SHA-256（`content_sha256`）を計算してメタに含めることを強く推奨（edge→central の整合検証に連動）。
    - ACK を受けるまでローカルの送信キャッシュを破棄しない。サーバから `duplicate_ignored` が返ればキャッシュを削除してよい。

- 任意だが有用
    - 端末のログに `payload_sha`, `local_seq`, `run_id`, `ack_timestamp` を残す。

3) 端末側での推奨 retry/timeout 設定
- attempts: 3〜5
- call/read/write timeout: 60〜120 秒
- backoff: 初期 2 秒、係数 2（例: 2s, 4s, 8s...）、maxDelay 300s
- jitter: ±10–30%（ランダム化）

4) 実装例（Kotlin / OkHttp 風の擬似実装）
（端末実装言語に合わせて移植してください。ポイントは「タイムアウト延長」「レスポンス本文のack判定」「content_sha256 の添付」「再試行」）

```kotlin
// 概念例（詳細は既存 client ライブラリに合わせる）
suspend fun uploadWithAck(..., runId: String?, localSeq: Int?, payloadBytes: ByteArray) {
    val sha = sha256Hex(payloadBytes)
    val client = baseClient.newBuilder()
        .callTimeout(120, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .writeTimeout(120, TimeUnit.SECONDS)
        .build()

    retrying { attempt ->
        val request = buildRequest(payloadBytes, mapOf("run_id" to runId, "local_seq" to localSeq?.toString(), "content_sha256" to sha))
        val resp = client.newCall(request).execute()

        if (!resp.isSuccessful) {
            val body = resp.body?.string()
            throw IOException("HTTP ${resp.code} body=$body")
        }

        val bodyStr = resp.body?.string() ?: throw IOException("Empty response")
        val json = JSONObject(bodyStr)
        val ack = json.optBoolean("ack", false)
        val status = json.optString("status", "")
        if (ack || status == "duplicate_ignored") {
            // 成功 -> ローカルキャッシュ削除
            return
        }
        throw IOException("No ack: $bodyStr")
    }
}
```

5) 端末側で実施する E2E テスト（必ず実施してください）
- 正常フロー：1 回送信で `ack:true` を受け取り、サーバ `logs/time_records` に `weights_received` が残ること。
- タイムアウト→再試行：送信中にネットワーク遮断して再接続、最終的に ACK を取得できること。
- 重複送信：同一 `content_sha256` を再送して server が `duplicate_ignored` を返すこと、端末は再送を止めること。
- event_timestamp の伝搬：端末で作った `event_timestamp` が edge→central を通して central のメタに残ること。

6) サーバ側に追加してほしい点（端末チームが要請する場合）
- edge が central に転送する際に `content_sha256` を必ず添付するパッチ（こちらで PR 作成可能）。
- central の run 選択ポリシー（同ラウンドで複数 run が届いた場合の勝者決定）についての運用ルール。

連絡先と次のアクション
- 端末担当にこの Markdown を渡して実装と E2E テストをお願いします。テスト結果（成功ログ＋端末ログの ACK 部分）をもらえれば私が central/edge のログと突合して確認します。
- 要望があれば、端末向けの短い Python テストスクリプトや Kotlin サンプルを追加で作成します。

---

必要ならこのファイルを短縮版（要点のみ）や PR 用の差分説明文に整形します。どちらが必要ですか？

---

付録: ローカルで使える付随スクリプト

- `scripts/terminal_client.py` — Python の端末クライアント例。SHA 計算、`run_id`/`local_seq`/`event_timestamp` 添付、指数バックオフ再試行をデモします。
- `scripts/terminal_client.ps1` — PowerShell の簡易サンプル。Invoke-RestMethod を使った再試行例を示します。
- `scripts/e2e_test.py` — 小さな E2E ヘルパー。`terminal_client.py` で upload を実行し、その後 `central_server/verify_metadata_consistency.py` を呼んで簡易検証を行います。

実行例 (ローカル):

```bash
# 端末クライアントを実行してエッジへ送信
python scripts/terminal_client.py --edge http://127.0.0.1:8001 --terminal-id sim-term-01 --round 1

# あるいは E2E ヘルパーを実行
python scripts/e2e_test.py --edge http://127.0.0.1:8001 --central http://127.0.0.1:8000
```

注: 上のスクリプトはローカル開発用のヘルパーです。本番端末への組み込みは各言語の HTTP ライブラリに合わせて移植してください。
