# クライアント・サーバ間の連携変更が必要な箇所

サーバサイドだけでは完結せず、**モバイル端末アプリ / エッジサーバとの合意が必要な変更**をまとめたドキュメント。

---

## 目次

1. [認証・認可](#1-認証認可)
2. [APIプロトコル](#2-apiプロトコル)
3. [エラーハンドリング](#3-エラーハンドリング)
4. [モデルファイル形式](#4-モデルファイル形式)
5. [WebSocket 通知](#5-websocket-通知)
6. [ラウンド状態同期](#6-ラウンド状態同期)
7. [訓練データ配信](#7-訓練データ配信)
8. [中央↔エッジ間通信](#8-中央エッジ間通信)

---

## 1. 認証・認可

### 1-A. Bearer token 形式の統一

**現状**

エッジサーバの `/receive_terminal_weights` には環境変数 `EDGE_UPLOAD_TOKEN` を使った簡易 Bearer 認証が存在する。
中央サーバへの全エンドポイントは認証なし（`auth_required: False`）。

```
edge_server/endpoints/terminal_update.py  行366–383
central_server/main.py  行407
```

**必要な変更**

| 対象 | 変更内容 |
|------|---------|
| サーバ | トークン検証ミドルウェアを中央・エッジ両サーバの全エンドポイントに追加 |
| サーバ | トークン発行 API `/api/v1/request_token` を新設 |
| クライアント | 全リクエストに `Authorization: Bearer <token>` ヘッダーを付与 |
| クライアント | 401 受信時のトークン再取得・リトライフローを実装 |

**影響エンドポイント**

- `POST /receive_terminal_weights/{terminal_id}` （エッジ）
- `POST /edge_update` （中央）
- `GET /download_global_model`, `GET /get_global_model` （中央）

---

### 1-B. WebSocket 認証

**現状**

WebSocket `/ws/updates` の認証はクエリパラメータ `?token=xxx` のみ。
HTTP の `Authorization` ヘッダーとは別の仕組みになっており、統一されていない。

```
edge_server/endpoints/notifications.py  行98–104
```

**必要な変更**

| 対象 | 変更内容 |
|------|---------|
| サーバ | WebSocket ハンドシェイク時に `Sec-WebSocket-Protocol` またはクエリパラメータでのトークン検証形式を決定・固定する |
| クライアント | 決定された形式でトークンを付与して接続 |
| クライアント | Close code `1008` (Policy Violation) 受信時の再接続フローを実装 |

---

## 2. APIプロトコル

### 2-A. `/receive_terminal_weights` のリクエスト形式

**現状**

端末→エッジの重み送信エンドポイントは `multipart/form-data` で複数の `payload_kind` / `dtype` に対応している。
デフォルトが `torch_state_dict` だが、どれを使うべきかが仕様書に記載されていない。

```
edge_server/endpoints/terminal_update.py  行263–295
```

フォーム項目（現状）：

| フィールド | 型 | 備考 |
|-----------|-----|------|
| `round_id` | int | 必須 |
| `model_id` | str | 必須 |
| `base_hash` | str | 必須 |
| `n_samples` | int | 必須 |
| `payload_kind` | str | `"full"` (デフォルト) |
| `dtype` | str | `"torch_state_dict"` / `"f32_flat"` / `"meta_bin"` |
| `input_size` | int | `f32_flat` 使用時に必須 |
| `hidden_size` | int | `f32_flat` 使用時に必須 |
| `output_size` | int | `f32_flat` 使用時に必須 |
| `contentSha256` | str | ファイルの SHA-256（任意） |
| `weights` | File | 重みファイル本体 |

**必要な変更**

| 対象 | 変更内容 |
|------|---------|
| サーバ | 推奨フォーマットを決定し、OpenAPI スキーマに明記する |
| クライアント | 決定されたデフォルトフォーマットで実装する |
| クライアント | `contentSha256` は必須化する方向で合意する |

---

### 2-B. 409 round_mismatch 後の再同期フロー

**現状**

端末のラウンドとエッジのラウンドが一致しない場合、エッジは `409 Conflict` + `retry_after: 5` を返す。
その後クライアントが何をすべきか（どのエンドポイントを叩くか）が明確でない。

```
edge_server/endpoints/terminal_update.py  行421–428
```

**409 レスポンス例（現状）**

```json
{
  "error_code": "round_mismatch",
  "detail": "Round mismatch. Please poll /api/v1/meta and retry.",
  "received_round": 2,
  "current_round": 3,
  "retry_after": 5
}
```

**必要な変更**

| 対象 | 変更内容 |
|------|---------|
| サーバ（エッジ） | 409 レスポンスに `meta_url` フィールドを追加し、同期先 URL を明示する |
| サーバ（エッジ） | `/api/v1/meta` のレスポンスに `model_download_url` を含める |
| クライアント | 409 受信 → `retry_after` 秒待機 → `meta_url` を叩いて round/model_id 更新 → 新モデル DL → 再アップロード というフローを実装 |

---

## 3. エラーハンドリング

### 3-A. エラーレスポンス形式の統一

**現状**

エンドポイントごとにレスポンス形式がバラバラ。

| 形式 | 使用箇所 |
|------|---------|
| `{"detail": "..."}` | HTTPException（FastAPI 標準） |
| `{"detail": "...", "errors": [...]}` | ValidationError |
| `{"error_code": "...", "detail": "...", "retry_after": N}` | 一部のエンドポイントのみ |

```
central_server/main.py  行178–196
edge_server/endpoints/terminal_update.py  行421
```

**必要な変更**

| 対象 | 変更内容 |
|------|---------|
| サーバ | `error_code`（機械可読）+ `detail`（人間可読）+ `retry_after`（任意）を統一スキーマとして全エンドポイントに適用 |
| クライアント | 上記スキーマに対応した統一パーサーを実装し、`error_code` で分岐 |

**統一スキーマ（提案）**

```json
{
  "error_code": "round_mismatch",
  "detail": "ラウンドが一致しません。メタを再取得してください。",
  "retry_after": 5
}
```

---

## 4. モデルファイル形式

### 4-A. torch.load の weights_only=True 強制化

**現状（修正済み）**

サーバ側では `weights_only=False` へのフォールバックを削除済み。
legacy `.tar` 形式（古い PyTorch）のファイルは 400 エラーで拒否するように変更した。

```
central_server/endpoints/edge_update.py  行684–707  ← 修正済み
edge_server/endpoints/aggregation.py  行438–450      ← 修正済み
```

**クライアントへの影響**

| 対象 | 必要な対応 |
|------|-----------|
| モバイルアプリ | `torch.save(state_dict, path)` で保存されたファイルのみ受け付けるため、**古い `.tar` 形式で保存していた場合は要修正** |
| モバイルアプリ | `torch.save` を使っている限り原則問題なし（PyTorch 1.6 以降のデフォルトは state_dict 形式） |

**確認事項**

- [ ] モバイル側の PyTorch（torchscript / torch mobile）バージョンを確認
- [ ] `torch.save(model.state_dict(), path)` で保存しているか確認
- [ ] `torch.save(model, path)`（モデルオブジェクトごと）は使っていないか確認

---

### 4-B. meta_bin フォーマット（weight.bin + meta.json）

**現状**

`dtype=meta_bin` として `weight.bin` + `meta.json` の2ファイルを送る形式が存在するが、
`meta.json` のスキーマが定義されていない。

```
edge_server/endpoints/terminal_update.py  行272–274
tools/pt_to_meta_weights.py
```

**必要な変更**

| 対象 | 変更内容 |
|------|---------|
| サーバ | `meta.json` の必須キー（`keys`, `shapes`, `dtype`, `schema_version`）をスキーマとして定義・公開 |
| クライアント | 定義されたスキーマに従って `meta.json` を生成 |

---

## 5. WebSocket 通知

### 5-A. 通知メッセージのフォーマット

**現状**

`broadcast_model_update()` で送信されるメッセージは以下の形式だが、JSON Schema として公開されていない。

```
edge_server/endpoints/notifications.py  行134–150
```

**現状のメッセージ構造**

```json
{
  "schema_version": "1",
  "notification_id": "uuid",
  "type": "model_update",
  "round": 2,
  "model_id": "...",
  "base_hash": "sha256:...",
  "download_rel": "path/to/model.pt",
  "timestamp": "20250326_143000"
}
```

**必要な変更**

| 対象 | 変更内容 |
|------|---------|
| サーバ | 上記スキーマを公式ドキュメント化し、`schema_version` のバージョニング方針を決める |
| クライアント | `notification_id` による重複排除処理を実装 |
| クライアント | `type` フィールドで分岐するハンドラを実装 |

---

### 5-B. ACK 機構

**現状**

サーバが通知を送信するが、クライアントが受け取ったことを確認する ACK の仕組みがない。
未受信の場合の再送ロジックも未実装。

```
edge_server/endpoints/notifications.py  行80–96
```

**必要な変更**

| 対象 | 変更内容 |
|------|---------|
| サーバ | `notification_id` ベースの ACK エンドポイント（`POST /notify_ack` は存在するが処理が空）を実装 |
| クライアント | 通知受信時に `notification_id` を含む ACK を送信 |
| クライアント | ACK 未送信で再接続した場合、`recent_notifications` から未送信分を再受信する処理を実装 |

---

## 6. ラウンド状態同期

### 6-A. `/api/v1/meta` レスポンスの拡張

**現状**

端末がラウンド情報を取得するための `GET /api/v1/meta` が存在するが、レスポンスに重要な情報が不足している。

```
central_server/main.py  行238–260
```

**現状のレスポンス**

```json
{
  "round": 1,
  "model_id": "demo-mlp-v1",
  "new_model_available": true
}
```

**必要な変更**

| 対象 | 変更内容 |
|------|---------|
| サーバ | `aggregation_threshold`（何台集まったら次ラウンドか）、`model_download_url` を追加 |
| クライアント | `model_download_url` を使って直接モデルをダウンロードするフローに対応 |

**拡張後レスポンス（提案）**

```json
{
  "round": 2,
  "model_id": "demo-mlp-v2",
  "new_model_available": true,
  "model_download_url": "/download_global_model",
  "aggregation_threshold": 3
}
```

---

## 7. 訓練データ配信

### 7-A. `/download_training_data` の大容量対応

**現状**

CSV ファイルをそのまま `FileResponse` で返している。大容量データへの対応がない。

```
central_server/endpoints/download.py  行125–133
```

**必要な変更**

| 対象 | 変更内容 |
|------|---------|
| サーバ | `Accept-Encoding: gzip` への対応、または Range リクエスト (`Content-Range`) の実装 |
| クライアント | gzip 解凍、または Range ダウンロードの実装 |

---

## 8. 中央↔エッジ間通信

### 8-A. エッジ→中央の集約済みモデル送信形式

**現状**

エッジが中央へ集約済みモデルを送信する際のリクエスト形式（multipart か JSON か）が
コードとドキュメントで一致していない部分がある。

```
edge_server/endpoints/aggregation.py
central_server/endpoints/edge_update.py
```

**必要な変更**

| 対象 | 変更内容 |
|------|---------|
| サーバ（中央） | `/receive_terminal_weights/{edge_id}` の受け付けフォーマットをエッジサーバ向けにも明文化 |
| サーバ（エッジ） | 集約後の送信コードを確認し、中央が期待する形式と一致させる |

---

## 優先度サマリ

| 優先度 | 項目 | 理由 |
|--------|------|------|
| 🔴 高 | **1-A 認証・認可の統一** | 現状は誰でもモデルを操作できる |
| 🔴 高 | **3-A エラーレスポンスの統一** | クライアントの自動リトライが実装できない |
| 🟠 中 | **2-B 409 再同期フローの明確化** | ラウンドずれ発生時にクライアントが詰まる |
| 🟠 中 | **4-A torch.load 形式の確認** | サーバ側修正済みなのでクライアントの確認が必要 |
| 🟠 中 | **5-A WebSocket 通知スキーマの確定** | 新ラウンド通知の信頼性に関わる |
| 🟡 低 | **6-A `/api/v1/meta` の拡張** | あると便利だが既存フローは動く |
| 🟡 低 | **7-A 訓練データの大容量対応** | データ量が増えてから対処でも可 |
| 🟡 低 | **5-B ACK 機構** | WebSocket 再接続時の信頼性向上 |

---

## クライアント実装チェックリスト

### モバイルアプリ

- [ ] `Authorization: Bearer <token>` ヘッダーの送信
- [ ] 401 受信時のトークン再取得・リトライ
- [ ] 409 `round_mismatch` 受信時のメタ再同期フロー
- [ ] 統一エラーレスポンスパーサーの実装
- [ ] `torch.save(state_dict, path)` 形式でのモデル保存（legacy `.tar` 形式を使っていないか確認）
- [ ] WebSocket 接続時のトークン付与
- [ ] WebSocket メッセージの `notification_id` 重複排除
- [ ] 通知受信後の ACK 送信

### エッジサーバ

- [ ] 中央サーバへのリクエストに Authentication ヘッダー追加
- [ ] 中央への集約済みモデル送信フォーマットの確認
- [ ] `POST /notify_ack` の処理実装
