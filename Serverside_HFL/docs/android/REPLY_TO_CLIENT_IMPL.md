# サーバ運用チーム向け返信（クライアント側テレメトリ／アップロード 実装 v1 受領）

以下、いただいたクライアント実装概要に対する整合確認と、推奨アクションの返信です。

## 整合性と受け側の状態
- エンドポイント整合:
  - `GET /api/v1/meta`: 429 時に `Retry-After` を返却。仕様通り。
  - `POST /receive_terminal_weights/{terminal_id}`: ラウンド不一致で 409／`current_round`／`next_meta_url=/api/v1/meta`／`Retry-After: 5`。仕様通り。
  - `POST /upload_client_logs/{terminal_id}`: multipart 任意パート対応、413 時 `Retry-After: 60`。仕様通り。
  - `POST /api/telemetry/ingest/{terminal_id}`: JSON 受領、過大時 413＋`Retry-After: 60`。仕様通り。
  - `GET /ws/updates?token=...`: 統一ペイロード（`schema_version=1`）＋`delay_hint_ms`。仕様通り。
- テレメトリ項目:
  - 受け側は主要フィールド（`round`, `timestamp_ms`, `battery_level`, `is_charging`, `temperature_c`, `cpu_usage`, `mem_used_mb`, `storage_free_mb`, `network_rssi`, `network_type`, `app_version`, `device_model`, `os_version`）を直接受領。その他（`device_manufacturer`, `os_sdk_int`, `wifi_*` 等）は `extras` で受け取り可能です。
- client_meta（学習結果アップロード）:
  - 現状、weights エンドポイントは必須メタ（`round_id`, `model_id`, `base_hash`, `n_samples`, `dtype` 等）を検証済み。追加の `client_meta` はサーバ側保存仕様を合意の上で拡張可能です。
  - ログアップロード側は Form キー `meta` を想定しています。`client_meta` を送る場合は `meta` に統一推奨です。

## 提案への回答
- (1) multipart text parts のリクエストログ出力: 賛成。サーバ側ログと突合しやすくなります。PII（SSID/BSSID 等）は端末側でマスク推奨。
- (2) TelemetryWorker の起動時自動スケジュール: 強く推奨。初期は 60 分間隔・充電中／Wi-Fi優先が良いです。収集を安定化します。
- (3) JSON バリデーション／スキーマ検証: 推奨。サイズ・型・範囲の事前チェックで 413/400 を減らせます。`client_meta` は 32KB など上限を共有するのが安全です。

## 運用面の注意
- ログ完全性: 端末側は「学習完了→flush/close→数秒待機→アップロード」を徹底（ローテーション済みのみ送信）。
- リトライ方針: 413/429 は `Retry-After` 厳守。その他は指数バックオフ（最大 60 秒、3〜5 回）。
- セキュリティ: Authorization ヘッダを自動付与。SSID/BSSID は運用ポリシー・同意に留意。

## 次に進めたいこと（優先順）
1. TelemetryWorker を起動時スケジュール（提案(2)）
2. リクエストデバッグログ強化（提案(1)、長文は上限＋マスク）
3. `client_meta` → `meta` 統一（ログAPI）、サイズ上限の合意（例: 32KB）

必要であれば、Kotlin の Retrofit インターフェース／WS ラッパ雛形を `docs/android/kotlin_samples/` に追加します。ご希望ください。