# 端末⇄エッジ（サーバ）インタフェース仕様書

この仕様書は、階層型連合学習における「端末（Client）」と「エッジ（Edge）／中央（Central）」の間でやり取りするAPI、配布アーティファクト、エラー挙動、テレメトリ項目、受け入れテストを最小かつ明確に規定します。

## 目的
- 端末が確実にモデルを取得・検証・適用できること
- 端末が重み（学習結果）を安全にアップロードできること
- 重複や遅延で学習が停止しない運用ルールを定めること

## 設計方針（研究向け最小実装）
- 実運用レベルの冗長性は不要だが、学習を止める致命的な挙動（無限リトライ、UI フリーズ、ラウンド整合性欠落）は防ぐ。
- 最小必須：`/send_to_device`（メタ）、`/download?rel_path=`（配信）、`/api/v1/meta`（ポーリング）、`/receive_terminal_weights/{terminal_id}`（重み受信）、`/upload_client_logs/{terminal_id}`（ログ受信）。

---

## 目次
1. エンドポイント仕様
2. 配布アーティファクト
3. アップロード挙動（重複・エラー）
4. テレメトリ／ログ仕様
5. 受け入れテスト
6. 最小実装と推奨追加（優先度）
7. 実装時間見積
8. サンプルリクエスト/レスポンス

---

## 1. エンドポイント仕様（必須）

### A. GET /send_to_device
- 目的: 端末が配布済みのモデル・教師データの場所と基本メタを取得する。
- 成功(200) 例:
```json
{
  "server_round": 31,
  "model_version": "v1.2.0",
  "links": {
    "model_meta": "https://edge.example.com/download?rel_path=20251215_121116/global_model_meta.json",
    "model_bin": "https://edge.example.com/download?rel_path=20251215_121116/global_weights.bin",
    "data": "https://edge.example.com/download?rel_path=20251215_121116/latest_data.csv"
  },
  "paths": {
    "model_rel": "20251215_121116/global_model_mobile.pt",
    "data_rel": "20251215_121116/latest_data.csv"
  }
}
```
- 準備中: 202 Accepted + `Retry-After` ヘッダ推奨（端末は Retry-After を尊重する）。
- 準備不能: 503 Service Unavailable（運用で合意）

### B. GET /download?rel_path=<url-encoded>
- 役割: 相対パスからファイルをストリーミング配信
- 挙動:
  - 存在 -> 200 + Content-Type + Content-Length
  - 不在 -> 404 Not Found
  - ファイル大 -> 413 Payload Too Large（可能なら Retry-After）
- セキュリティ: Authorization: Bearer ... を推奨

### C. GET /api/v1/meta
- 役割: ポーリング用メタ（ラウンド同期）
- 必須フィールド:
```json
{ "round": 31, "model_rel": "...", "model_version":"v1.2.0", "aggregation_completed": true }
```
- 端末は `round` でサイクル継続・停止の判定を行う。

### D. POST /receive_terminal_weights/{terminal_id}
- 役割: 端末から学習後の重みを受け取る
- multipart/form-data。必須フィールド:
  - `round_id` (int)
  - `model_id` (string)
  - `base_hash` (string)
  - `n_samples` (int > 0)
  - file `weights` (バイナリ)
- 推奨フィールド:
  - `payload_kind` (default `full`), `dtype`, `run_id`(uuid), `local_seq`(int), `client_meta`(JSON)
- レスポンス:
  - 成功: 200 {"ack":true, "status":"ok", "sha256":"..."}
  - 重複受理: 200 {"ack":true, "status":"duplicate_ignored", ...}  ※端末はこれを成功扱いする
  - round mismatch: 409 {"detail":"round_mismatch", "server_round": <int>}
  - 大きすぎ: 413 + Retry-After
  - 内部エラー: 500

### E. POST /upload_client_logs/{terminal_id}
- 役割: 端末のログとテレメトリを受け取る
- multipart: `meta` (JSON string), `file_0..file_n` (最大5, 1ファイル<=10MB推奨)
- メタに `round` と `run_id` を含めることを強く推奨

---

## 2. 配布アーティファクト

### meta.json（推奨最低スキーマ）
- content:
  - model_version: string
  - dtype: string (e.g. "float32")
  - byte_order: "little" | "big"
  - layout: ["layer.weight", "layer.bias", ...]  // state_dict キー順
  - shapes: { "layer.weight": [out,in], ... }
  - content_sha256: hex
  - expected_size_bytes: int
  - created_at: ISO8601
- 役割: 端末が weights.bin を正しく分割してテンソル化するためのガイド

### weights.bin（推奨簡易形式）
- 推奨: `layout` 順に float32（IEEE754, little-endian）を連続格納したフラットバイナリ
- メリット: 端末実装が容易で軽量
- 代替: 簡易ヘッダ付きテンソル列や npz など（ただし端末でのデコード実装コストが増える）

### .pt（TorchScript / full model）
- 付与しても可。端末が PyTorch Mobile/Libtorch を使っている場合はそのまま利用可能。ただし研究用途で端末学習を行うなら meta+bin の方が軽量で汎用的。

---

## 3. アップロード挙動（重複・エラー）

- 重複キー: sha256(content) + terminal_id + round
- サーバは重複を検出したら **200 + {"status":"duplicate_ignored"}** を返すことを推奨する。端末はこれを成功扱いし、再試行を止める。
- 端末側バックオフ:
  - 初期間隔 5s、指数バックオフ（5s→10s→20s）、最大 3 回
  - OkHttp タイムアウト: connect/read/write = 60s 推奨
- 429/413 が返った場合、`Retry-After` があればそれを尊重して再試行

---

## 4. テレメトリ / ログ項目（端末→サーバ）

### 必須（端末は最低限送る）
- terminal_id (string)
- run_id (uuid)
- round (int)
- model_version (string)
- n_samples (int)
- total_local_ms (int)
- epoch_durations_ms (array of int)
- accuracy (float, % or 0..1)  // 実装に合わせて統一
- loss (float)
- client_meta (JSON string)

### 任意（可能なら含める）
- tp_mean,tp_std,rtt_mean,rtt_std
- network_transport ("wifi"|"cell"|...)
- current_edge_id,target_edge_id,change_effective_round
- ping_stats (min/mean/max)

サーバは欠損(null)を許容するが、可能な限り端末は空欄を減らすこと。

---

## 5. 受け入れテスト（簡易 E2E）

シナリオA (正常)
1. サーバに model_meta + model_bin を配置
2. 端末 GET /send_to_device → links.model_bin を取得
3. 端末は SHA/size を検証してファイルを保存
4. 端末学習 → POST /receive_terminal_weights/{terminal_id} → 200 OK
5. サーバ集約→ round 更新
6. 端末が /api/v1/meta の round を検出 → 新 model をダウンロードして適用

シナリオB (ファイル未準備)
- サーバは /send_to_device で 202 + Retry-After を返す。端末は Retry-After 待機後再試行。

シナリオC (重複)
- 端末が再送してもサーバは `200 {"status":"duplicate_ignored"}` を返し端末は再送を止める。

---

## 6. 最小実装（研究用途）と推奨追加

### 最小実装（必須・短期）
- サーバ: /send_to_device が `links.model_bin`（絶対URL）を返す
- サーバ: /receive_terminal_weights が duplicate の場合 200+`duplicate_ignored` を返す
- クライアント: upload の 400 body に "weights size mismatch" や "duplicate" を含む場合は成功扱い
- クライアント: upload 再試行を 5s→10s→20s（最大3回）に変更
- クライアント: upload 成功/duplicate の後に /api/v1/meta をポーリングして次ラウンドを待ち、新モデルを必ずダウンロードして適用

### 推奨追加（運用をよくする）
- meta.json スキーマを確定して必須化
- weights.bin を flat float32 形式で統一
- /send_to_device は準備中に 202+Retry-After を返す
- テレメトリフィールドを厳格化して空を減らす取り組み

---

## 7. 実装時間見積（研究用途、1名）
- Hotfix (duplicate handling + backoff + timeouts): 0.5–1 日
- クライアント: ラウンド待機＋新モデルダウンロード: 1–2 日
- UI: Start ボタンロック／ログ自動送信: 0.5–1 日
- 簡易テスト・修正: 0.5–1 日
- 合計（楽観）: 約2–3 日、（現実）: 4–6 日

---

## 8. サンプルチケット（サーバ実装担当向け）
件名: 【対応依頼】/send_to_device に model_meta/model_bin の links を追加 & duplicate 応答の明確化
本文（要点）:
- /send_to_device のレスポンスに `links.model_meta` と `links.model_bin` を常に含めてください（絶対URL）。
- POST /receive_terminal_weights/{terminal_id} で重複を検出した際は HTTP 200 と body {"status":"duplicate_ignored"} を返すようにしてください。端末はこれを成功扱いします。
- 可能なら配布未準備時は 202 + Retry-After を返すようにしてください（端末が適切に待機できます）。

---

## 9. 付録: 簡単なダウンロード検証フロー（端末側）
1. GET /send_to_device → links.model_bin 取得（優先）
2. GET model_bin URL（タイムアウト 60s）
3. ファイル保存→ content_sha256（もし提供されていれば）を検証
4. 失敗時: 5xx/timeout → 再試行（指数バックオフ）

---

作成者: automated spec generator
作成日: 2025-12-17
