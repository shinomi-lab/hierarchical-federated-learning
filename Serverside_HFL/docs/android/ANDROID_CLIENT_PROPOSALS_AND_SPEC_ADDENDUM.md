# 付録: テレメトリ必須フィールド／client_meta 上限／レスポンス標準化（合意案）

本付録は `ANDROID_CLIENT_PROPOSALS_AND_SPEC.md` の補強として、合意済み方針を明記します。

## テレメトリ必須フィールドと補完
- 必須: `timestamp_ms`（欠落時はサーバ受信時刻で補完）、`device_model`（欠落時は`"unknown"`補完）、`network_type`（欠落時は`"UNKNOWN"`補完）
- 推奨必須: `os_sdk_int`（欠落時はNULL保存を許容）
- その他（`wifi_*` 等）: 省略または`null`で可。追加情報は `extras` に格納可能。

## client_meta サイズ上限
- 上限: 8KB（UTF-8バイト長）。超過時は 413 を返却（`Retry-After: 60`）。ボディ例:
```json
{"error":"client_meta_too_large","retry_after":60}
```
- 端末側で事前トリム推奨。

## Form キー命名の統一（ログAPI）
- 端末側メタは Form キー `meta` に統一（例 `{ "round":31 }`）。`client_meta` を使う場合はログAPIでは `meta` に合わせることを推奨。

## レスポンス標準化の方針
- 成功:
  - テレメトリ: `HTTP 200` + `{ "ack": true, "status":"ok" }`
  - ログ: `{ "ack":true, "status":"stored|duplicate_ignored" }`
  - weights: `{ "status":"weights_received", "ack": true }`
- エラー: 可能な範囲で `{ "error":"<code>", "detail":"<message>", "retry_after": <sec?> }` を返却（段階的に移行）。

## 送信前チェック（例）
```kotlin
require(deviceModel.isNotBlank())
val payload = JSONObject().apply {
  put("timestamp_ms", System.currentTimeMillis())
  put("device_model", deviceModel)
  put("network_type", networkType)
  put("os_sdk_int", Build.VERSION.SDK_INT)
}
// 8KB超過しないように、client_metaやextrasは要トリム
```
