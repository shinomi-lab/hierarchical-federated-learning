# 端末実装検証チェックリスト

端末実装者へ投げて「本当に実装されているか」検証するためのチェックリストです。疑義が出たら端末実装者にログ/リクエスト/レスポンスを提示してもらってください。

## 1) 基本エンドポイント確認（動作と形式）
- [ ] `GET /send_to_device` は200で `links.model_meta` と `links.model_bin`（または `links.model`）を返すか？
  - CLI: `curl -sS https://EDGE/send_to_device | jq .links`
  - 期待: JSON に `model_meta` と `model_bin` のURL（文字列）が存在。
- [ ] `GET /api/v1/meta` は200で `round`, `model_id`, `upload_endpoint` を返すか？
  - CLI: `curl -sS https://EDGE/api/v1/meta | jq .`
  - 期待: `round` は整数、`upload_endpoint` が `/receive_terminal_weights/{terminal_id}`。

## 2) meta.json の取得・検証
- [ ] 端末は `model_meta` をGETして JSON Schema（`format_version`,`weights_size`,`weights_sha256`,`tensors`）を検証しているか？
  - CLI: `curl -sS "https://EDGE/download?rel_path=.../weight.meta.json" -o meta.json && jq . meta.json`
  - 期待: `weights_size` は整数、`weights_sha256` は 64 文字 hex、各 tensor に `name,shape,offset,length_bytes` がある。
- [ ] `weights_size` と HTTP `Content-Length` を突合しているか。

## 3) weight.bin の取得・整合性
- [ ] 端末は `weights_sha256` を計算して `meta.json` の値と一致するまで `weight.bin` を受け入れないか？（強制）
  - CLI: `curl -sS "https://EDGE/download?rel_path=.../weight.bin" -o weight.bin && sha256sum weight.bin`
  - 期待: sha256 == meta.weights_sha256
- [ ] 部分ダウンロード（Range）や途中切断のリトライを実装しているか（大容量対策）。

## 4) バイナリ→テンソル復元の正確さ
- [ ] リトルエンディアンかつ `float32` として読み、`shape` に reshape しているか？（`bytes_per_element=4` を前提）
- [ ] `length_bytes == prod(shape)*bytes_per_element` を検証しているか。失敗時は破棄してエラー扱いにするか。

## 5) state_dict 組立とモデル適用
- [ ] 生成した `state_dict` を `load_state_dict`（またはモバイルライブラリ相当）で正常にロードできるか検証したか？ロード後 NaN/Inf チェックを行っているか。
  - 簡易チェック: 復元したテンソルの sum/min/max をログで出力してもらう。

## 6) upload（端末→エッジ）仕様適合
- [ ] 端末は `POST /receive_terminal_weights/{terminal_id}` に対して、下記のいずれか正しい形式を送れるか？
  - `dtype=torch_state_dict` + `.pt` file
  - `dtype=f32_flat` + `.bin`/`.npy` + required dims
  - `dtype=meta_bin` + multipart: `weights` (weight.bin) + `meta_file` (meta.json)
- [ ] 送信時に `contentSha256`（または `Content-Sha256`）を付け、サーバの計算値と一致しない場合にリジェクトされることを確認したか。
  - 試験: 成功時 200 + JSON `{status:"weights_received", sha256: "sha256:..."}`、不一致なら 400/422。

## 7) 認証・許可・RateLimit
- [ ] `EDGE_UPLOAD_TOKEN` が設定されている場合は `Authorization: Bearer <token>` を付けないと 401 が返るか？
  - 試験: 省略で 401 を確認、付与で 200 を確認。
- [ ] `/api/v1/meta` のレート制限（429 + Retry-After）の扱いを実装しているか（respect するか）。

## 8) ラウンド同期・競合
- [ ] 端末の `round_id` と `/api/v1/meta` の `round` が不一致ならアップロードが 409 を返すことを確認しているか（端末は再同期→再送すること）。

## 9) エラー時の挙動（冪等性）
- [ ] 重複アップロードのACK（duplicate）を正しく扱い、二重送による不整合が発生しないか（サーバが duplicate を検知しても集約判定は行う仕様）。
- [ ] ネットワーク障害時の再試行ロジック（指数バックオフ＋最大再試行回数）を実装しているか。

## 10) ロギング・テレメトリ
- [ ] ダウンロード・検証・アップロードの各ステップで少なくとも（file name, bytes, sha, error_code）をログに出しているか。運用側が事後調査できるようログを残すこと。

## 11) セキュリティ（推奨）
- [ ] 将来的な改ざん対策として `meta.json.signature` を受け入れ・検証できる準備があるか（必須でないが推奨）。
- [ ] 証明書/HTTPS、トークンの保管は安全にしているか。

## 12) 実行テスト（短く実行してもらう）
端末側で下記を実行してログを提示してもらう（回答サンプルを要求）:
1. `curl GET /send_to_device` の出力（JSON）
2. `curl GET /download?rel_path=.../weight.meta.json` の JSON（先頭）
3. `sha256(weight.bin)` と `meta.weights_sha256` の一致確認ログ
4. `POST /receive_terminal_weights/{terminal_id}` を `dtype=meta_bin` で実行したときのリクエストヘッダとレスポンス（成功 or エラー詳細）
5. アップロード時に `round` をずらしたケース→サーバが 409 を返すことのスクリーンショット/ログ

## 13) 端末に提示する「失敗時に提出してもらう情報」
- `curl -v` の完全リクエスト/レスポンス（ヘッダ含む）。
- ダウンロードした `meta.json` と `sha256(weight.bin)` の出力。
- アップロード時の multipart フィールド一覧とファイルサイズ、そしてサーバから返された JSON（エラー含む）。

## 短い検証コマンド（例）
- GET send_to_device:
  - `curl -sS https://EDGE/send_to_device | jq .`
- Download meta/bin:
  - `curl -sS "https://EDGE/download?rel_path=PATH/weight.meta.json" -o meta.json && jq . meta.json`
  - `curl -sS "https://EDGE/download?rel_path=PATH/weight.bin" -o weight.bin && sha256sum weight.bin`
- Upload meta+bin:
  - `curl -v -X POST "https://EDGE/receive_terminal_weights/device-001" -F "round_id=1" -F "model_id=test" -F "n_samples=1" -F "dtype=meta_bin" -F "weights=@weight.bin" -F "meta_file=@meta.json" -H "Authorization: Bearer <token>"`
