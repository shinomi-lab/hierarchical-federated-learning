# Postman: HFL Server-side collection

このフォルダには `Server_side_Postman_Collection.json`（Postman v2.1 コレクション）が含まれます。研究や手動検証のために以下のリクエストが含まれます:

- `GET /api/v1/meta` — エッジのメタ情報取得
- `POST /receive_terminal_weights/{terminal_id}` — torch_state_dict / f32_flat のアップロードサンプル

使い方:

1. Postman を開き、`Import` → `File` で `Server_side_Postman_Collection.json` を読み込みます。
2. 環境（Environment）を作り、以下の変数を設定します（このコレクションは ngrok を使用しないローカル / 移動先切替前提に `PROTOCOL`/`HOSTNAME`/`PORT` を使います）:
   - `PROTOCOL`（`http` か `https`。ローカルで ngrok を使わない場合は `http`）
   - `EDGE_HOSTNAME`（例: `localhost` またはローカルIP）
   - `EDGE_PORT`（例: `8001`）
   - `CENTRAL_HOSTNAME`（例: `localhost`）
   - `CENTRAL_PORT`（例: `8000`）
   - `EDGE_TOKEN`（必要なら Bearer トークン）
   - `TID`, `MODEL_ID`
3. コレクションのリクエストを実行して挙動を確認します。ngrok を使わない想定であれば、環境を切り替えるだけで大学やテザリング環境でも `EDGE_HOSTNAME`/`EDGE_PORT` を更新して使えます。大きいファイルをアップロードする場合は Postman の動作が重くなるので、`curl` やスクリプトでの検証を推奨します。

注意:
- `contentSha256` は端末側で事前に計算することを推奨します。Postman 内で大容量ファイルを JS でハッシュ計算するのは現実的でないことがあります。
