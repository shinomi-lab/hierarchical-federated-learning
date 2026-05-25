# Claude Code 引き継ぎ: ラウンド1完了後のスタック & エラーレポート 400 Bad Request

実機で「**全端末がラウンド1の学習は完了したが先に進まない**」と「**エラーレポートをサーバに送ると HTTP 400**」が同時に出ている場合の、**サーバ側で疑う箇所と変更候補**を整理する。  
（クライアントは `POST {edge}/upload_client_logs/{terminal_id}` にマルチパートで送る。）

---

## A. ラウンド1のままスタックする（統合・中央の挙動）

### A1. 中央が「複数エッジ分の `edge_update`」を待っている（最有力）

- **根拠:** `Serverside_HFL/central_server/endpoints/edge_update.py` は `round_updates[round]` に届いた **エッジ数**が `CENTRAL_AGGREGATION_THRESHOLD` 以上になるまでグローバル集約を **キューに入れない**（未達なら `accepted_and_waiting`）。
- **既定:** `central_server/config.py` の `CENTRAL_AGGREGATION_THRESHOLD` は環境変数なしだと `1` だが、`Serverside_HFL/start.py` で **エッジ台数に合わせて未設定時のみ上書き**される運用がある（`RUN_GUIDE.md` に「`--edges 2` のとき 2」等の記載）。
- **症状:** 実験が **エッジ2台登録**・閾値 **2** のとき、**エッジ01だけ**が端末から集約して `edge_update` しても、**エッジ02から同一ラウンドの更新が来ない限り**中央は待ち続ける。端末側は round 1 の学習は終わっているが **新グローバルが出ない** → メタの `waiting_for_next_round` 等で **スタックに見える**。

**サーバ側の変更・運用候補**

1. **`CENTRAL_AGGREGATION_THRESHOLD=1` を明示**（単一エッジでもグローバル進行させる）。研究目的が「必ず全エッジ分を混ぜたい」場合だけ 2 にする。
2. または **エッジ02に端末がいないラウンドでは「空更新」やダミー重みを送る設計**は非推奨 → 閾値と登録エッジ数のポリシーを文書化し、`start.py` の自動設定ロジックを見直す（例: 「登録エッジ数」と「集約に必要なエッジ数」を分離）。

### A2. グローバル配布 `receive_model` がエッジ02で失敗している

- **根拠:** 過去ログで `POST .../receive_model` が **500**（`global_model_mobile.tmp` → `.pt` の rename で `ENOENT`）。同一バッチへの **二重プッシュ**の疑いあり。
- **症状:** 中央は集約完了後も **エッジ02が最新モデルを取れない**。端末はエッジごとに挙動が分かれ、**一方のエッジ配下だけ進まない**。

**サーバ側の変更候補**

- `edge_server` の `receive_model` 保存処理: **`mkdir` + 一意 tmp + 冪等（同一 batch の重複 POST）**。
- 中央の `async_send_model_and_app`（`central_server/endpoints/edge_management.py`）: **同一エッジへの二重 POST**を防ぐ。

### A3. エッジ側 `waiting_for_new_model` とポーラー

- **根拠:** `edge_server/endpoints/terminal_update.py` / `model_ops.py` で **`waiting_for_new_model`** が立ち、中央から新モデルが来るまで端末の重み受付とラウンド進行がブロックされる経路がある。
- **症状:** 中央が A1/A2 で進まない → エッジが **ずっと待機** → 端末が **ラウンド1のまま**。

**サーバ側の変更候補**

- 中央の集約・配布が完了したことをエッジが確実に検知できるよう、**ポーラー・メタ `round`・`waiting_for_next_round`** の整合をテスト（既に `api/v1/meta` に `waiting_for_next_round` を足す修正があるなら、その上で中央が meta を更新しているか確認）。

### A4. エッジ01上で「端末1台分のアップロードが未完」

- **根拠:** 最新ログ例で `terminal-01` が **`weights_receive_start` のみ複数回**で完了イベントが無いケースあり。バケット閾値が 2 のとき **集約が発火しない**可能性。

**サーバ側（＋端末）**

- タイムアウト・チャンクサイズ・423 リトライ。エッジの **受信タイムアウト**とログの相関調査。

---

## B. エラーレポート送信が HTTP 400 になる（`upload_client_logs`）

エンドポイント: **`Serverside_HFL/edge_server/endpoints/client_logs.py`** の `POST /upload_client_logs/{terminal_id}`。

### B1. サーバが返す 400 の公式パターン（コード上）

| 条件 | 応答概要 |
|------|-----------|
| `await request.form()` が失敗 | `400` `bad_request` — **invalid or unreadable multipart body** |
| `UploadFile` として認識されるファイルパートが 0 | `400` `bad_request` — **no files provided**（`form_parts` をイベントログに出す処理あり） |
| パートはあるが **全ファイルが 0 バイト**（読み込み後 `received` が空） | `400` `bad_request` — **no non-empty log files (0-byte uploads rejected)** |

**403/401** は別経路（`TERMINAL_ALLOWLIST_JSON` で拒否、`EDGE_UPLOAD_TOKEN` 不一致）。ユーザーが **400** と言っているなら上表を優先して疑う。

### B2. クライアント実装（突合せ用）

- `HFL_terminals/.../network/NetworkClient.kt` — `uploadClientLogs` は `file_0`, `file_1`, … と **`meta`** をマルチパートで送信。
- `HFL_terminals/.../work/UploadLogsWorker.kt` — エラーレポートは **`report_kind: "error_report"`** の JSON を `meta` に入れる。スナップショットファイルが空なら **送信前に IOException**（クライアント側で 400 にならない）のため、**400 は「リクエストは届いたがサーバが拒否」**であることが多い。

### B3. サーバ側で検討すべき変更（400 対策）

1. **`no files provided` 対策（互換性）**  
   - Starlette が **`UploadFile` 以外**（例: `multipart/form-data` のパートがファイル扱いにならない Content-Disposition）として捨てている可能性を調査。  
   - **緩和案:** `form.multi_items()` で **文字列/バイトのファイルパート**も受け取り、一時ファイルに書くフォールバック（フィールド名 `file_*` 限定など）。

2. **`invalid or unreadable multipart body` 対策**  
   - リバースプロキシの **body サイズ制限**、**chunked の切れ**、**Content-Type boundary 不一致**を疑う。  
   - **観測強化:** `form_parse_error` に **`Content-Type` / `Content-Length` / 先頭数バイト（hex）** を載せる（個人情報に注意）。

3. **`no non-empty log files` 対策**  
   - 端末が **0 バイトのプレースホルダ**だけ送っている場合。  
   - **緩和案:** エラーレポート専用で **`report_kind == error_report` のときは空ファイルをスキップして 200 + `received: []`** にする（運用で「必ず1ファイル以上」要求するならドキュメント化）。

4. **許可リスト**  
   - `TERMINAL_ALLOWLIST_JSON` を使っている環境では **403** になるが、ミドルウェアやゲートウェイが **403→400 に変換**していないか確認。

5. **トークン**  
   - `EDGE_UPLOAD_TOKEN` 設定時は **401**。クライアントが `AppConfig.getServerAuthToken` を載せているか、`NetworkClient` 生成時に **Authorization ヘッダ**が `uploadClientLogs` に付くか（現コードでは **付いていない**可能性 → トークン必須環境では **401** だが、別経路で 400 になることもあり得るため要確認）。

---

## C. 推奨デバッグ手順（短時間）

1. 中央ログで該当ラウンドの `edge_update` 応答が **`accepted_and_waiting` か `accepted_and_queued` か**を確認（A1）。  
2. 登録エッジ数と **`CENTRAL_AGGREGATION_THRESHOLD`** の実効値を `start.py` / 環境変数から確認。  
3. エッジの `client_logs` イベントログ（`form_parse_error` / `no_files` / `no_nonempty_files`）で **400 の内訳**を特定（B1）。  
4. 端末の `RealTimeLogger` に **`uploadClientLogs failed: code=400 body=...`** が残っていれば、その **JSON の `detail`** をサーバの `error_response` 形式と突合。

---

## D. 関連ファイル一覧

| 領域 | パス |
|------|------|
| 中央・エッジ待ち | `Serverside_HFL/central_server/endpoints/edge_update.py`（`CENTRAL_AGGREGATION_THRESHOLD`） |
| 閾値既定 | `Serverside_HFL/central_server/config.py` |
| 起動時 env | `Serverside_HFL/start.py` |
| エッジ→中央送信 | `Serverside_HFL/edge_server/endpoints/aggregation.py` |
| モデル配布 | `Serverside_HFL/central_server/endpoints/edge_management.py`、`edge_server/endpoints/model_ops.py`（`receive_model`） |
| 端末ログ・エラーレポート | `Serverside_HFL/edge_server/endpoints/client_logs.py` |
| 端末送信 | `HFL_terminals/.../NetworkClient.kt`、`UploadLogsWorker.kt` |
| メタ・待機 | `Serverside_HFL/edge_server/endpoints/terminal_update.py`（`/api/v1/meta`） |

---

以上。先に作成した `docs/claude_handoff_hfl_federation_issues.md`（P0–P4 の一覧）と併せて渡すとよい。
