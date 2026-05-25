# Android client — Server requirements and API contract

このドキュメントは Android (Kotlin/OkHttp) で端末側のクライアントを実装するために必要なサーバ側の要件と API 契約をまとめたものです。

目的
- 端末がローカル学習後にエッジサーバへ重みをアップロードできること
- 端末がエッジ／中央からのダウンロード（モデル、設定、教師データ）を取得できること
- 再試行・タイムアウト・メタデータの取り扱いを明確にする

想定サービス構成（既存実装に基づく）
- 中央サーバ (Central): `http://<central-host>:8000`
  - 例: `/rounds/start`, `/download_training_data`, `/edge_update` など
- エッジサーバ (Edge): `http://<edge-host>:8001`
  - 端末は通常エッジにアップロードし、エッジが集約して中央へ送る

主要エンドポイント（現状の実装を踏まえた推奨一覧）
- GET /orchestration/status
  - 返却: JSON { "round": int, "model_id": str, "aggregation_threshold": int }
  - 目的: 端末が現在のラウンドや条件を確認する

- POST /orchestration/start
  - リクエスト JSON: { "round_id": int, "aggregation_threshold": int (optional), "metadata": {...} }
  - 目的: (主に中央→エッジで使う) エッジにラウンド開始を通知する

- POST /receive_terminal_weights/{terminal_id}
  - マルチパート multipart/form-data
  - fields:
    - `weights` (file; application/octet-stream) — PyTorch の state_dict を `torch.save()` した .pt（もしくはシリアライズ済みバイナリ）
    - `n_samples` (int) — この端末で学習に用いたサンプル数（重みの重み付けに利用）
    - `sig` (str, optional) — アップロードファイルの sha256 等の整合性情報
    - `manifest` (file, optional) — 学習メタデータ（JSON）
    - `metadata` (JSON string, optional) — 任意の追加メタデータ（端末モデル、バージョンなど）
  - レスポンス: JSON { "ok": true, "message": "received", "edge_round": int }
  - 備考: サーバ側は受信したファイルをディスクに保存し、更新レコードをキューに入れる

- GET /download_training_data (central)
  - 返却: CSV やアーカイブファイル（Content-Type: text/csv or application/zip）
  - 目的: エッジが教師データを取得する場合に使う

- GET /get_global_model or /download_global_model (central)
  - 返却: model file (application/octet-stream)
  - 目的: エッジや端末が初期モデルや最新グローバルモデルを取得する

- POST /aggregate_and_push (edge/operator)
  - リクエスト JSON: { "round_id": int, "model_id": str (optional), "auto_send": bool, "interactive": bool }
  - 目的: エッジ側で集約を行い、中央への送信を行う

ファイル形式と実装決定事項
- 重みファイル形式
  - 推奨: PyTorch の `state_dict` を `torch.save(state_dict, file)` で保存したバイナリ（.pt）
  - 受信側（エッジ）は `torch.load()` または独自のデシリアライズを行い、テンソルを CPU float32 に変換して処理する
- manifest / metadata
  - JSON 形式で、少なくとも `model_version`, `dataset_hash`, `training_epochs`, `n_samples` を保管することを推奨

ネットワーク要件（接続・タイムアウト・再試行）
- 接続タイムアウト: 10秒（初回接続）
- リクエストタイムアウト: 120秒（大きなモデルアップロードのため）
- 再試行: エッジへのアップロードは指数バックオフで最大 3 回を推奨
- 冪等性: 端末は再送時に同じ `sig`/`manifest` を付与し、サーバは重複を検出して重複受信を無視または上書きできるようにする

認証とセキュリティ
- 推奨: HTTPS を必須にする
- 認証: トークンベース (Bearer token / API key) を推奨。端末は一意のクライアント ID と API key を持つ。
- ファイル整合性: アップロード時に `sig`（sha256）を付与し、サーバ側で検証する

Android 実装ノート（OkHttp/Retrofit）
- Retrofit + OkHttp を使う場合のポイント:
  - multipart upload を使って `weights` ファイルと `manifest` を同時に送信
  - long timeout: OkHttpClient で connect/read/write timeout を 2 分程度に設定
  - 再試行: ネットワーク失敗時は指数バックオフで最大 3 回
  - レスポンスの JSON をデシリアライズするために Moshi/Gson を使用

エラーハンドリングの契約
- 2xx: 正常（レスポンス JSON に `{ "ok": true }` のようなフィールドを含める）
- 4xx: クライアント側エラー（無効な manifest、フォーマット不正など）
- 5xx: サーバ側エラー（再試行推奨）

バックグラウンドでの動作
- Android では WorkManager を使ってアップロードタスクを実行すると堅牢
- Foreground Service が必要なほど大きなファイルを扱う場合は通知付きの Foreground ワークを推奨

運用上の注意点
- ファイルサイズ: モデルが大きい場合、edge 側で chunked upload を実装するか、直接クラウドストレージ（S3）にアップロードして、サーバにはメタだけ渡すアーキテクチャを検討
- Locale/ファイル名: Windows/Android 間でのファイル名エンコーディング問題に注意（ASCII のみ推奨）

追加で提供できるもの（要望に応じて作成）
- Retrofit のサンプル Kotlin コード（multipart upload, retry, backoff）
- WorkManager を使ったアップロードジョブの雛形
- サーバのエンドポイントごとの OpenAPI/Swagger スキーマ（既存の FastAPI から自動生成可能）

---

次のアクション: この `ANDROID_SERVER_REQUIREMENTS.md` をリポジトリに作成しました。内容を見て追加してほしい箇所があれば教えてください。これで Android 側実装を始める準備が整います。