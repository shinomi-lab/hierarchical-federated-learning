## ngrok を使ったリモート開発・ホットスポット環境の手引き

目的: スマホのテザリングや外部ネットワークからローカルで動かすサーバ（`edge_server` / `central_server`）に安全にアクセスし、誰でも同じ手順で接続・検証できるようにするための手順書です。

**概要（まず結論）**
- あなたのローカルで `uvicorn` が起動している（例: `127.0.0.1:8001` / `127.0.0.1:8000`）ことを前提に、`ngrok` がローカルから ngrok のクラウドへアウトバウンド接続を張ります。
- ngrok が成功すると `https://xxxx.ngrok.io` のような公開 URL（Forwarding URL）が発行され、その URL へ来たリクエストが ngrok によりあなたのローカルにフォワードされます。
- ngrok プロセスを終了すると紐付けは消えます（無料プランだと再起動で URL が変わる点に注意）。

**重要ポイント: URL の寿命と紐付け**
- ngrok の公開 URL は "ngrok プロセスが ngrok クラウドに接続している" 間だけ有効です。`Forwarding` 行が表示され `online` なら有効です。
- 永続的に同じサブドメインを使いたい場合は ngrok の有料プランや Cloudflare Tunnel、自己ホストのリバースプロキシを検討してください。

**対応サーバ**
- エッジサーバ: `edge_server`（推奨ローカルポート例: `8001`）
- 中央サーバ: `central_server`（デフォルト: `8000` — 実装側の `central_server/main.py` の uvicorn 呼び出しを参照）

## **中央サーバの ngrok 設定**

**実行手順（central_server を公開）**
1. プロジェクトルートへ移動
```bash
cd /Users/tetsuya/HFL/Serverside_HFL
```

2. `uvicorn` で中央サーバを起動（別ターミナル）
```bash
# 例: central_server のデフォルトは port 8000
python -m uvicorn "central_server.main:app" --host 127.0.0.1 --port 8000 --reload
```

3. ngrok で公開トンネルを立てる（別ターミナル）
```bash
ngrok http 8000
```

4. 成功確認
- ターミナルに `Forwarding https://xxxx.ngrok.io -> http://localhost:8000` の行が表示される。
- ngrok の管理 UI: `http://127.0.0.1:4040` でトンネルと直近のリクエストを確認。

**複数サービス（central と edge）を同時に公開したい場合**
- 無料プラン: 各ローカルポートに対して別々に `ngrok http <port>` を実行すると、それぞれ別の公開 URL が発行されます（2つのターミナルで起動）。
- 有料プラン: 予約サブドメインや TLS 終端、カスタムドメインを割り当てられるため、運用上楽になります。

## **端末 ↔ エッジ間の設定（包括ガイド）**

以下は `edge_server` 側のエンドポイント仕様と、端末クライアントで推奨される実装方針です。

**主要エンドポイント（edge 側）**
- GET `/api/v1/meta`
	- 端末が学習開始前に取得するメタ情報（現在の round、model_id、upload_endpoint 等）。
	- エッジは IP ベースのレートリミットを持つ（デフォルト: 1 req / 2 秒）。
- POST `/receive_terminal_weights/{terminal_id}`
	- 端末が学習済み重みを送る API。必須フォームフィールド:
		- `round` (int), `model_id` (str), `base_hash` (str), `n_samples` (int)
		- `dtype` (例: `torch_state_dict` または `f32_flat`)
		- `weights` (multipart file)
	- 任意: `contentSha256`（端末側で計算した sha256 を渡すとエッジで検証・デデュープ利用可）
	- `f32_flat` を用いる場合は `input_size`, `hidden_size`, `output_size` を必須で送ること。
	- レスポンスは ACK オブジェクト（`status`, `ack`, `ack_timestamp`, `sha256`, `round_id`, など）を返す。

**認証 / トークン**
- エッジ側は環境変数 `EDGE_UPLOAD_TOKEN` が設定されている場合、`Authorization: Bearer <token>` ヘッダーを必須化します。実装は `edge_server/endpoints/terminal_update.py` にあり、ヘッダが無い、または値が不一致なら 401 を返します。
- 推奨: 端末にトークンを埋め込む際は安全な配布方式（MDM／シークレットストア等）を使い、公開リポジトリやログにトークンを残さないでください。トークンは定期的にローテーションする運用が望ましいです。

**デデュープと署名検証**
- クライアントは通信前にファイルの SHA-256 ハッシュを計算し、フォームフィールド `contentSha256` に `sha256:<hex>` 形式で渡すと、サーバ側で検証されます。ミスマッチだと 400 を返します。
- サーバは受信ハッシュを永続的ファイルに保存して重複を検出します（重複時は `duplicate_ignored` ACK を返す）。

**クライアント実装のベストプラクティス**
- 軽量かつ堅牢な実装手順（擬似フロー）:
	1. GET `https://<EDGE_HOST>/api/v1/meta` を取得して `upload_endpoint` と `round` 等を読む。
	2. 学習を実行 → 生成された重みファイルをストリームで送信。
	3. （必須ではないが推奨）ファイルの sha256 を計算し、`contentSha256` にセットする。
	4. POST multipart/form-data で `receive_terminal_weights/{terminal_id}` に送信。`Authorization: Bearer <token>` を追加。
	5. 200 ACK を受け取り、`sha256` / `ack_timestamp` を保存してリトライ時の判定に使う。

**curl の例: torch_state_dict を送る**
```bash
# 事前に変数をセット
NG="<NGROK_HOST>"; TID="terminal-123"; TOKEN="<EDGE_UPLOAD_TOKEN>"
curl -v -X POST "https://$NG/receive_terminal_weights/$TID" \
	-H "Authorization: Bearer $TOKEN" \
	-F "round=1" \
	-F "model_id=demo-mlp-v1" \
	-F "base_hash=sha256:bootstrap" \
	-F "n_samples=100" \
	-F "dtype=torch_state_dict" \
	-F "weights=@./local_weights.pt;type=application/octet-stream" \
	-F "contentSha256=sha256:<hex>"
```

**curl の例: f32_flat（.bin）を送る**
```bash
curl -v -X POST "https://$NG/receive_terminal_weights/$TID" \
	-H "Authorization: Bearer $TOKEN" \
	-F "round=1" \
	-F "model_id=demo-mlp-v1" \
	-F "base_hash=sha256:bootstrap" \
	-F "n_samples=50" \
	-F "dtype=f32_flat" \
	-F "payload_kind=full" \
	-F "input_size=128" -F "hidden_size=64" -F "output_size=10" \
	-F "weights=@./flat_weights.bin;type=application/octet-stream" \
	-F "contentSha256=sha256:<hex>"
```

**HTTP レート制御・リトライ**
- `/api/v1/meta` は IP ベースでレート制限（1 req / 2 秒）を行うため、頻繁なポーリングは避けて exponential backoff を使ってください。
- 重複送信に備えて、クライアントは ACK の `ack_timestamp` と `sha256` を保存し、再送時はサーバの重複レスポンスを正しく扱えるようにします。

**サーバ側のログで確認すべきイベント**
- `weights_bytes_received`, `weights_saved_deferred`, `weights_parsed`, `weights_received` などのイベントを `edge_time_logger` に記録しています。ngrok 経由でも uvicorn のコンソール（またはログファイル）に `Request: METHOD PATH` のログが出ます。

## **セキュリティと運用上の注意**

- トークン管理: `EDGE_UPLOAD_TOKEN` を設定している場合は必ず利用する。トークン漏洩時は即座にローテーションする手順（新トークン発行 → エッジへ反映）を作る。
- ログ: 公開トンネルでは外部からアクセスされるため、`traceback` や秘密をログに出力しない運用ポリシーを徹底する。本リポジトリの `central_server` は開発用に例外の traceback を返す箇所があるため、本番化時は調整する。
- 同時公開: central と edge の両方を公開するときは、異なるポートで ngrok を起動するか、有料プランでカスタムドメイン/サブドメインを割り当てる。

## **付録：よく使うコマンドまとめ**
```bash
# edge_server 起動 (例: 8001)
python -m uvicorn "edge_server.main:app" --host 127.0.0.1 --port 8001 --reload

# central_server 起動 (例: 8000)
python -m uvicorn "central_server.main:app" --host 127.0.0.1 --port 8000 --reload

# ngrok トンネル（各サーバのポートごとに別シェルで実行）
ngrok http 8001   # edge 用
ngrok http 8000   # central 用

# ngrok 管理 API でトンネル確認
curl -s http://127.0.0.1:4040/api/tunnels | python3 -m json.tool

# healthz 確認
curl https://<NGROK_HOST>/healthz

# meta 確認
curl https://<NGROK_HOST>/api/v1/meta
```

---
作成日: 2025-11-25
作成者: HFL Server_helper (編集済み — 必要に応じて運用方針をチームで調整してください)
