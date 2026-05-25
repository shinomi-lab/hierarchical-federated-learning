**端末向け: 推論結果受領仕様**

目的: エッジサーバが実行した推論結果（AP割当等）を端末が確実に受け取り、必要に応じてモデルを取得・検証して適用するための最小実装仕様。

- **前提（サーバ側提供）**:
  - WebSocket エンドポイント: `/ws/updates`（クエリ `token` をサポート）
  - 推論エンドポイント（サーバ内）: `/edge/infer` — ラウンド閾値 (`EDGE_INFERENCE_MIN_ROUND`, デフォルト 5) を満たす場合に実行
  - モデル取得: `/download?rel_path=<rel>` および管理用 `/send_to_device` を提供
  - ACK 受信用: HTTP POST `/notify_ack`（notification_id, client_id）

- **配信メッセージ形式**:
  - ブロードキャスト JSON（例）:
    {
      "type": "switch_ap",
      "payload": {
        "final_assign": [int,...],
        "model_rel": "<ts>/<filename>.pt",    # 任意だが存在すればダウンロード可能
        "timestamp": "YYYYMMDD_HHMMSS"
      }
    }

- **端末が実装すべきこと（チェックリスト）**:
  - 1) WebSocket 常時接続
    - 接続先: `ws://<edge_host>/ws/updates?token=<WS_AUTH_TOKEN>`（`WS_AUTH_TOKEN` 無設定時は任意）
    - 自動再接続（指数バックオフ）と接続状態監視
  - 2) メッセージ受信と検証
    - 受信した JSON の `type` を分岐し、`switch_ap` を処理
    - `payload.final_assign` の長さと自端末のインデックスの対応方法を事前合意
  - 3) 割当取得・適用
    - 自端末に対応するインデックスを決め、`assigned_ap = final_assign[index]` を算出
    - AP 切替 API を呼ぶかデバイス内ロジックで反映
  - 4) モデル取得（`model_rel` がある場合）
    - ダウンロード方法: GET `http(s)://<edge_host>/download?rel_path=<model_rel>`（または `/send_to_device` でリンク取得）
    - ダウンロード後は `meta.json` がある場合に `content_sha256` と `expected_size_bytes` を検証
    - バイナリが TorchScript/.pt の場合、モデルをロードして必要なら変換
  - 5) ACK / テレメトリ
    - 受信確認やダウンロード完了をサーバへ通知する場合、`POST /notify_ack` を使う（`notification_id`, `client_id` を送る）
  - 6) テレメトリ（任意フィールド）
    - 端末は追加の運用指標を任意で送れます。推奨フィールド:
      - `mem_used_mb`: 浮動小数（MB）、プロセスまたはデバイスの使用メモリ
      - `mem_total_mb`: 浮動小数（MB、任意）
      - `mem_free_mb`: 浮動小数（MB、任意）
      - `mem_percent`: 浮動小数（0-100、任意）
      - `process_rss_mb`: 浮動小数（プロセス RSS、任意）
      - `process_vms_mb`: 浮動小数（プロセス VMS、任意）
    - サーバはこれらを欠損許容で受け取り、存在する場合はそのまま保存・集計します。
    - 単位を必ず明記してください（上記は MB 単位）。
  - 6) 冪等性と重複処理
    - 同一 `notification_id` を複数回受け取っても問題ないようにガード

- **詳細仕様 / 注意点**:
  - ブロードキャストは fire-and-forget: 接続していない端末には届かない。常時 WS 接続が必要。
  - `final_assign` の順序（端末配列）についてはシステム運用で合意しておく（例: 起動時に中央が配布する端末一覧のインデックス順）。
  - モデル配布の流れ: サーバが `model_rel` を通知 -> 端末は `/download` で取得 -> `content_sha256` を検証 -> 必要に応じて再起動やホットロード。
  - 認証: `WS_AUTH_TOKEN` をエッジで設定している場合、必ず token を付与して接続すること。

- **エラー・フォールバック**:
  - モデル検証失敗: ダウンロードを破棄し、サーバにログ（/notify_ack でエラーコード）を送るかローカルでフォールバック実装
  - メッセージ破損/不正: 何もしないでログ記録。サーバへの再要求は運用次第

  追記: 端末側でメモリ計測を行う場合、高頻度で送るとバッテリ／ネットワーク消費に影響するため、間引きやサンプリングを推奨します。

- **軽量 Python 受信クライアント（例）**
```python
import asyncio, websockets, json

async def run():
    uri = 'ws://127.0.0.1:8001/ws/updates?token=MYTOKEN'
    async for ws in websockets.connect(uri):
        try:
            async for msg in ws:
                j = json.loads(msg)
                if j.get('type') == 'switch_ap':
                    payload = j['payload']
                    # self_index は端末毎に既定
                    assigned = payload['final_assign'][self_index]
                    # apply assignment...
        except Exception:
            await asyncio.sleep(1)

asyncio.run(run())
```

- **運用チェックリスト（配布前）**:
  - エッジで `EDGE_INFERENCE_MIN_ROUND` を期待値に設定済みか
  - `WS_AUTH_TOKEN` とクライアントのトークンを配布済みか
  - 端末インデックスの割当ルールがドキュメント化されているか
  - モデル検証（sha256/サイズ）と失敗時の運用フローが定義されているか

実装状況短評: サーバ側は以下の機能を提供済みで、端末実装で受信可能な状態です。
  - WebSocket ブロードキャスト (`/ws/updates`) と `broadcast_model_update` 実装
  - 推論実行と broadcast 発行（`/edge/infer`） — ラウンド閾値チェックあり
  - モデル配布エンドポイント (`/download`, `/send_to_device`) と `notify_ack` ハンドラ

残注意点（運用上の確認）:
  - 通知に使う `model_bin`/`meta.json` がエッジ上に存在していること
  - 端末インデックスの合意方法（サーバは順序で配列を返すが、端末側で対応付け必須）
  - 大量端末時の遅延ヒント（`WS_DELAY_HINT_MS`）利用運用

ファイル: [docs/TERMINAL_RECEIVE_INFERENCE_SPEC.md](docs/TERMINAL_RECEIVE_INFERENCE_SPEC.md)
