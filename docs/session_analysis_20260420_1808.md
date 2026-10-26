# セッション分析メモ（2026-04-20 18:08 頃）

実機・サーバログに基づく整理。関連ログ: `Serverside_HFL/logs/time_records/central_server_20260420_180831.log`, `edge_server_edge-server-01_20260420_180832.log`, `edge_server_edge-server-02_20260420_180834.log`、クライアントログ受信イベントは `Serverside_HFL/logs/events/edge_client_logs/20260420.jsonl`。

---

## 1. グローバルラウンドが進まないように見える理由（推定）

- 中央は同一セッションで **エッジが2台登録**（8001 → `edge_count: 1`、8002 → `edge_count: 2`）。
- **edge-server-01** は round 1 で **terminal-03 と terminal-02** の2クライアント分を集約し、`sent_to_central` **HTTP 200** で `/edge_update` 済み（`sum_n_samples: 4376`）。
- この時点の **edge-server-02** 側ログは、当該ウィンドウでは端末からの重み受信・集約の記録がほぼ無い（実質アイドル）。
- 中央の `central_server_20260420_180831.log` では **`model_received`（edge-server-01）までは出ているが、その後のグローバル集約完了・全エッジへのプッシュ等の行は当該ファイル断片には無い**。
- 運用設定で **`CENTRAL_AGGREGATION_THRESHOLD` が登録エッジ数（2）と一致**している場合、**2台目の `edge_update` が来るまで中央集約が始まらない**挙動になり得る。実験を「エッジ1台＋端末2台」で回すなら、閾値を 1 にするか、2台目のエッジを止めて登録数を揃える必要がある（`Serverside_HFL/start.py` の `select_thresholds` / 環境変数参照）。

---

## 2. 端末1（terminal-01）の画面が「終わった感じがしない」こととの対応

### サーバ側の事実（18:08 セッション・edge-01）

- **terminal-03 / terminal-02**: `weights_receive_start` → `weights_bytes_received` → … → `weights_received` まで一連で記録あり。
- **terminal-01**: **`weights_receive_start` が同一 round で複数回**出ているが、**`weights_bytes_received` 以降が無い**（アップロードが途中で止まっているか、接続が切れて再試行している状態と整合的）。

そのため、端末1では **ACK を返すまでにサーバが受信完了していない**可能性が高く、アプリ側も「成功した」と明確に出せない経路になりやすい。

### アプリ側（補足）

- 重みアップロード成功時も `TrainingViewModel.uploadUpdateSuspend` の **`finally` で `_uploadProgress` を 0 に戻している**ため、プログレスバーが一瞬 100% になってもすぐ消える（「送信完了」の視覚的な残り方が弱い）。
- 学習終了時は `TrainingScreen` で **Toast**（学習完了＋自動/手動アップロード案内）がある一方、**送信完了専用の大きな完了 UI は限定的**。

改善するなら: terminal-01 の転送安定化（ネットワーク・タイムアウト）に加え、**アップロード成功時の明示メッセージ／プログレス保持**などが有効。

---

## 3. エラーレポート（`upload_client_logs`）は送れているか

### 2026-04-20 のイベントログ（`edge_client_logs/20260420.jsonl`）

| 端末 | 時刻（ログ上） | イベント | 解釈 |
|------|----------------|----------|------|
| terminal-01 | 15:31:45 | `no_files` | `report_kind` が **`error_report` でない**通常アップロード扱いで、サーバは **`no files provided`（HTTP 400）** を返す分岐。当該日の **18:08 付近の terminal-01 の client_logs 行は無い**。 |
| terminal-02 | 複数回 | `no_files` | 同上パターン（multipart にファイルパートが見えるが処理上「有効ファイル無し」扱いになっているログ）。 |
| terminal-02 | 18:09:55 | `error_report_no_files_accepted` | メタのみのエラーレポートとして **HTTP 200 相当で受理**（`ack: True`, `status: no_files_received`）。バッチ ID `20260420_180955_541410`。 |

### ディスク上の保存先（`received_files/terminal_logs/error_reports/`）

- **terminal-02**: `20260420_180955_541410/` に `README.txt` と `request_meta.json` あり（上記イベントと一致）。**メタのみ受理でログファイル本体は無い**。
- **terminal-01**: 同日付の `error_reports/terminal-01/20260420_*` は **無し**（少なくともこのワークスペースの受信ディレクトリでは 18:08 セッションのエラーレポート保存は確認できない）。2026-04-19 の batch は存在。

### 結論（ユーザー質問への答え）

- **「エラーレポートがサーバに届いたか」**  
  - **terminal-02** については、**18:09:55 にエラーレポート（ファイル無し）として受理された記録とディレクトリがある** → **届いている（内容はメタ中心）**。  
  - **terminal-01** については、**20260420 の `edge_client_logs` には 18:08 前後の受信が無く、`error_reports/terminal-01/` にも同日分が無い** → **このログ・受信ディレクトリの範囲では「18:08 セッションでエラーレポートが届いた」とは言えない**。同日午前の bulk 系は `no_files` で **400 側**のログが残っている。

サーバ実装の要点（`edge_server/endpoints/client_logs.py`）:

- `report_kind == "error_report"` かつ **multipart にファイルが1つも無い**場合でも **200** でディレクトリ＋README を残す。
- それ以外でファイルが無い場合は **`no_files` 警告のあと 400**。

端末側でエラーレポートを押したときは `UploadLogsWorker` が `report_kind: error_report` を付ける（`HFL_terminals/.../UploadLogsWorker.kt`）。**通常の post-upload ログ送信（bulk）** とは別経路のため、混同すると 400/200 の差が出る。

---

## 4. 次にやるとよい確認・対処

1. **中央集約が止まる場合**: `CENTRAL_AGGREGATION_THRESHOLD` と **実際に重みを送るエッジ台数** を一致させる。
2. **terminal-01 の重み**: 端末の `adb logcat` / `upload_logs.csv` / `RealTimeLogger` で **タイムアウト・切断** を確認。サーバは **`weights_receive_start` のみ**ならボディ未着。
3. **ログ・エラーレポート**: エラーレポートボタン後に **`edge_client_logs` の `client_logs_upload_done` または `error_report_*`** が出るか、および `received_files/terminal_logs/error_reports/<terminal_id>/` に batch が増えるかを見る。

---

## 5. 目標と改善メモ（全端末からエラーレポートを確実に受け取る）

### 目標（受け入れ条件のイメージ）

- **全端末 ID**（例: `terminal-01` … `terminal-03`）について、ユーザーが「エラーレポート／ログ送信」を実行したときに、**必ずエッジの `POST /upload_client_logs/{terminal_id}` が成功（2xx）**し、**`received_files/terminal_logs/error_reports/<terminal_id>/<batch>/` に痕跡が残る**（ファイル付きなら実ファイル、無ければ現仕様どおりメタ＋ README でも可）。
- 運用で **`TERMINAL_ALLOWLIST_JSON` を使っている場合**は、**全端末がいずれかのエッジの許可リストに含まれる**こと（含まれない端末は 403 で弾かれる）。

### 現状とのギャップ（このメモで分かっていること）

- **経路の混同**: `report_kind` が `error_report` でない POST は、ファイルパートがサーバ側で「有効なファイル無し」と判定されると **400**（ログ上の `no_files`）。エラーレポート用ボタンは **`UploadLogsWorker` の `snapshotsOnly` で `error_report` を付ける**経路に乗せる必要がある（`TrainingViewModel.uploadCurrentLogFile` → `enqueueErrorReportOnce`）。
- **端末間差**: terminal-02 は `error_report_no_files_accepted` で受理の実績がある一方、terminal-01 は同日の **受信ディレクトリに batch が無い**時間帯があり、**ネットワーク未着・別エッジ・未実行・401/403** など切り分けが必要。
- **中身がメタのみ**: スナップショット元が空だと「受理はされるがログ本文は無い」になり得る。調査用途なら **スナップショット生成前に学習／RealTimeLogger 初期化を一度通す**、または端末側で「送信対象ゼロ」を明示して再試行させる、といった整理が必要。

### 実装・運用チェックリスト（追って対応する項目）

| 領域 | 内容 |
|------|------|
| **端末（Android）** | 各端末の `terminal_id` と **接続先 baseUrl（エッジ URL）** が一致しているか。エラーレポートは重みと同じエッジへ送る前提でよいか確認。 |
| **端末** | `prepareErrorReportSnapshots` の元ファイル（`realtime_events.log` / `model_update_log.txt` / `training_log.csv`）が **0 バイトでない**状態で送信しているか。空なら現状はメタのみ or 送信スキップになり得る。 |
| **端末** | `EDGE_UPLOAD_TOKEN` をサーバと揃えているか（未設定ならサーバ側もトークン無しでよいが、**片方だけ設定**だと 401）。 |
| **サーバ** | `edge_server/endpoints/client_logs.py` で、`no_files` ログに **`form_parts` に `UploadFile` が並んでいるのに `files` が空**となるケースがあれば、multipart パースと `isinstance(..., UploadFile)` の整合（Starlette / FastAPI の型差・バージョン差）を調査し、**ファイルパートを確実に列挙**する修正を検討する。 |
| **サーバ** | 成功時に必ず **`client_logs_upload_done`** または **`error_report_*`** をイベントログに残し、**HTTP ステータス**と **terminal_id** を対で追えるようにする（現状よりトレースしやすくする）。 |
| **運用** | `TERMINAL_ALLOWLIST_JSON` を使うなら **全端末 ID を明示**（空リストのエッジは「全端末許可」だが、リストを置いたエッジでは漏れが即 403）。 |
| **検証** | 端末ごとに 1 回ずつエラーレポートを押し、**`logs/events/edge_client_logs/YYYYMMDD.jsonl`** と **`received_files/terminal_logs/error_reports/`** を見て **全端末分の batch** があることを確認する。 |

### メモの位置づけ

上記は **本ドキュメントに紐づく「やりたいこと」の要求メモ**であり、コード変更は別コミットでこのチェックリストに沿って進める想定とする。

### 5.1 実装メモ（追記・コード反映）

以下をコードに反映済み（本 MD を前提としたフォローアップ）。

| チェックリスト行 | 対応内容 |
|-------------------|----------|
| サーバ・multipart 列挙 | `client_logs.py` に `_is_multipart_file_part` / `_collect_multipart_files` を追加し、`UploadFile` 以外のファイルパートも拾う。 |
| サーバ・トレース | `client_logs_upload_done` / `error_report_*` / `no_files` / `no_nonempty_files` 等に **`http_status`** と **`reason`（reject 系）** を付与。403/401/413/400 で `client_logs_reject` または既存イベントを拡張。 |
| 端末・メタのみ | `UploadLogsWorker` がスナップショット **0 件でも** `report_kind: error_report` のみ POST する。`NetworkClient.uploadClientLogs` が **ファイル 0 件＋ error_report メタ**の multipart を許可。 |
| 端末・UI | `TrainingViewModel.uploadCurrentLogFile` が **0 件でもキュー投入**。`TrainingScreen` の成功ダイアログ判定に **`Upload queued`** を追加。 |

未着手（運用・環境依存）: `TERMINAL_ALLOWLIST_JSON` の全端末明示、`CENTRAL_AGGREGATION_THRESHOLD` とエッジ台数の整合、terminal-01 の重み転送の実ネット調査。

---

## 参照パス

| 種別 | パス |
|------|------|
| 中央ログ（該当セッション） | `Serverside_HFL/logs/time_records/central_server_20260420_180831.log` |
| エッジ01 | `Serverside_HFL/logs/time_records/edge_server_edge-server-01_20260420_180832.log` |
| エッジ02 | `Serverside_HFL/logs/time_records/edge_server_edge-server-02_20260420_180834.log` |
| クライアントログ API イベント | `Serverside_HFL/logs/events/edge_client_logs/20260420.jsonl` |
| 受信ストレージ | `Serverside_HFL/received_files/terminal_logs/`（`error_reports/` 配下） |
