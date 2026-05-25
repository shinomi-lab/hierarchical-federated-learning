# WebSocket Notification Specification (Edge → Android)

この仕様は、エッジサーバから端末へ新モデル到着やラウンド更新を通知するためのWebSocketプロトコルを定義します。Androidクライアントは本仕様に従うことで、安定した受信・適用・計測が可能になります。

- エンドポイント: `ws://<EDGE_HOST>/ws/updates`
- 認証: 環境変数 `WS_AUTH_TOKEN` が設定されている場合、`?token=<WS_AUTH_TOKEN>` をクエリに付与
- メッセージ形式: テキストJSON
- Ping: 端末が `{"action":"ping"}` を送ると、サーバは `{"type":"pong"}` を返します

## 1. 通知ペイロード

- 外形: `{"type":"model_update", "payload": { ... }}`
- `payload` フィールド（統一スキーマ）
  - `schema_version`: `1` （整数）
  - `notification_id`: `UUID` 文字列（ACKやイベント紐付け用）
  - `round`: `int | null` （新しいグローバルラウンド番号。中央→エッジ受領時は +1 済）
  - `download_rel`: `string | null` （例: `YYYYMMDD_HHMMSS/global_initial_mobile.pt`）
  - `download_url`: `string | null` （直叩き可能な絶対URL。未設定ならクライアント側で組み立て）
  - `description`: `string | null` （`manual_push` / `auto_increment_on_receive_model` など）
  - `timestamp`: `string` （通知生成時刻。`YYYYMMDD_HHMMSS`）

備考:
- ダウンロードはHTTPで提供。相対パスの場合、端末はベースURLから構築して取得してください。
- 将来的な拡張に備え、未知フィールドは無視する実装を推奨します。

## 2. クライアント動作（推奨フロー）

1. 接続: `ws://host/ws/updates?token=...`（必要に応じて）
2. 受信: `type == "model_update"` を検出→`payload.round`で最新ラウンドを把握
3. メタ同期: `GET /api/v1/meta` を即時取得（HTTPフォールバックの一元化）
4. 取得: `download_url` があれば直接DL、なければ `download_rel` からURL組み立て
5. イベント送信: `POST /device_event`
   - `MODEL_LOAD_START` / `MODEL_LOAD_END`（ダウンロード〜ロード）
   - `APPLY_START` / `APPLY_READY`（適用開始〜準備完了）
6. ACK送信: `POST /device_ack` に `notification_id` を含めて送信（遅延計測に利用）
7. 再接続: 切断時は指数バックオフ＋ジッターで再接続。復帰後に `ping` で健全性確認。

## 3. エラーとフォールバック

- WS不可時: 5〜10秒間隔で `GET /api/v1/meta` をポーリング
- 409（重みアップロード）: レスポンスに `Retry-After: 5` と `next_meta_url` が付与されるため、従う
- 429（レート超過）: 端末側は同様に`Retry-After`を尊重
- 413（サイズ超過）: 分割・圧縮・選別で送信量を削減

## 4. Android 実装例（Kotlin）

```kotlin
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import okio.ByteString
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject

class UpdatesSocket(
    private val baseUrl: String,
    private val token: String?
) : WebSocketListener() {
    private var ws: WebSocket? = null
    private val client = OkHttpClient()

    fun connect() {
        val url = if (token != null) {
            "$baseUrl/ws/updates?token=$token"
        } else {
            "$baseUrl/ws/updates"
        }
        val req = Request.Builder().url(url).build()
        ws = client.newWebSocket(req, this)
    }

    override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
        webSocket.send("{\"action\":\"ping\"}")
    }

    override fun onMessage(webSocket: WebSocket, text: String) {
        val obj = JSONObject(text)
        if (obj.optString("type") == "model_update") {
            val p = obj.getJSONObject("payload")
            val round = p.optInt("round")
            val nid = p.optString("notification_id")
            val rel = p.optString("download_rel")
            val absUrl = p.optString("download_url", null)
            // TODO: fetch /api/v1/meta, then download model via absUrl or constructed URL from rel
            // TODO: POST /device_event ...
            // TODO: POST /device_ack with notification_id = nid
        }
    }

    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
        // TODO: reconnect with backoff
    }

    override fun onFailure(webSocket: WebSocket, t: Throwable, response: okhttp3.Response?) {
        // TODO: reconnect with backoff
    }
}
```

## 5. 互換性

- サーバは既存のブロードキャスト箇所を統一済み（`broadcast_model_update`）。
- 旧クライアントはトップレベルに `round` などが来るケースにも対応しておくと安全。（当面は`payload`を最優先で読む）

---

この仕様により、端末は通知→メタ同期→ダウンロード→適用→ACKまでの流れを確実に実行でき、サーバ側は通知からACK/イベント計測までを一貫して管理できます。