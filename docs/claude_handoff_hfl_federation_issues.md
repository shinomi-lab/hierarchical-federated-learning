# Claude Code 引き継ぎ: HFL 連合まわりの不具合・調査結果（2026-04-20 ログ基準）

このファイルは Cursor でのログ横断の結果をまとめたもの。**Claude Code にそのままコンテキストとして渡す**用途向け。

## 参照ログ（ワークスペース内）

- `Serverside_HFL/logs/time_records/central_server_20260420_*.log`
- `Serverside_HFL/logs/time_records/edge_server_edge-server-01_20260420_*.log`
- `Serverside_HFL/logs/time_records/edge_server_edge-server-02_20260420_*.log`
- `Serverside_HFL/logs/rounds/r*/edge_server_edge-server-01_20260420_*.log`（当日分）

Docker 当日ログはヒットなし（比較対象: `docker/logs/compose_20260416_*.log` は別日）。

---

## 問題リスト（優先度順・同一根の症状として整理）

### P0: 中央 → エッジ2（8002）`receive_model` が 500（モデル保存失敗）

- **現象:** `POST .../receive_model` が **500**。`detail` に `global_model_mobile.tmp` → `global_model_mobile.pt` の **rename で ENOENT**。
- **ログ例:** `central_server_20260420_153136.log`, `163231.log`, `163519.log`（15:33 / 16:33 / 16:36 付近）。同一タイミングで **8002 に対し 200 と 500 が両方**出る行もあり、**二重 POST / 競合**の疑い。
- **影響:** エッジ2に接続する端末が **最新グローバルを取り損ねる・遅延・リトライ依存**。2 エッジ構成の同期破綻の直接因。
- **修正の当たり:** `Serverside_HFL/edge_server/` の `receive_model` 保存処理、`central_server` の **複数エッジへの push が二重に走らないか**（`edge_update` 後のタスク等）。

### P1: エッジ集約が「複数端末の 1 本 FedAvg」にならない（`run_id` 分裂・二重 `edge_update`）

- **現象:** 同一 `round`・同一エッジで **`num_clients: 1` の `edge_update` が連続**（別 `run_id`）。バケットが閾値に達した直後に **`aggregation_already_scheduled`（warning）**。
- **ログ例:** `edge_server_edge-server-01_20260420_172135.log`（17:22:42 付近）、当日 `rounds/r*` の JSONL にも `aggregation_already_scheduled` が複数回。
- **影響:** 連合としての重み付き平均が意図通りにならない／**後から届いた 1 クライアント分がグローバルを上書き**し得る。学習曲線が「おかしい」に見える主因の一つ。
- **修正の当たり:** `Serverside_HFL/edge_server/endpoints/terminal_update.py`（集約スケジュール・`run_id` 別キャッシュ `_aggregation_cache/run_*`）、端末側 **同一実験で `run_id` / `session_id` を揃える**か、エッジで **ラウンド単位にのみ集約**するかの設計判断。

### P2: ラウンド・状態の一貫性欠如（中央 vs エッジ、端末間）

- **現象:** `graceful_reset` 時に **中央 `round_before: 31`、エッジ `round_before: 26`** のように **同時刻でもラウンドが一致しない**。以前の分析では **ラウンド二重バンプ**（ローカルサイクル完了ハンドラ + バックグラウンドポーラ）の指摘あり（`terminal_update.py`）。
- **影響:** 端末の表示ラウンド・サーバのラウンド・実モデルがずれ、**同期崩れ**に見える。
- **修正の当たり:** `terminal_update.py` のラウンド進行の **単一ソース化**、既に入った修正の有無の確認・テスト。

### P3: アップロード未完・欠損

- **現象:** `weights_receive_start` のみ複数回、**`weights_bytes_received` / `telemetry_received` が続かない**（例: `terminal-02`, 17:22 台、`edge_server_edge-server-01_20260420_172135.log`）。
- **影響:** そのラウンドの集約に端末が入らない・タイムアウト・リセットで中断など。
- **修正の当たり:** 端末のタイムアウト・チャンクアップロード、エッジの受信タイムアウト、423 リトライ（会話メモ参照）。

### P4: `/upload_training_metrics` が 200 だが中身がすべて null

- **現象:** `training_metrics_received` で `accuracy`, `loss`, `satisfaction_*` 等が **すべて null**（複数セッションで反復）。
- **影響:** レポート・実験記録が事実上使えない（「破綻」に見えるのは評価面）。
- **修正の当たり:** エッジから中央へのメトリクス POST の **フォームフィールド組み立て**（`client_logs` 周り等）、端末からエッジへのメタ受け渡し。

### P5: モデル配布パスのフォールバック（`global_model_mobile.bin` → `weight.bin`）

- **現象:** `download_path_fallback` がエッジ JSONLに出る。
- **影響:** 動いていても **契約が曖昧**で将来の不具合源。
- **修正の当たり:** 端末の要求ファイル名とエッジの正規パスを **統一**。

### P6（参考）: `virtual_router_id` と実 SSID の混同しやすさ

- **現象:** テレメトリの `virtual_router_id` が **`HFL_A24` 固定に近い**（`TrainingViewModel.currentRouterId` 等）。実際のエッジ選択は **WiFi SSID → 8001/8002**（`AppConfig.kt`）。
- **影響:** ログ解釈の混乱（ルータ名とエッジ ID の取り違え）。連合バグの主因というより **観測ラベルの不整合**。
- **修正の当たり:** `virtual_router_id` を **現在 SSID または論理 AP 名**と同期。

---

## 推奨ワーク順（Claude Code 向け）

1. **P0** `receive_model` の保存を **mkdir + 一意 tmp + 冪等（同一 batch の重複 POST）** で堅牢化。中央の **二重 push** があれば排除。
2. **P1** エッジ集約を **ラウンド単位でスケジュール 1 回**、`run_id` 分裂時の挙動を仕様化してコードに反映。
3. **P2** ラウンド進行を **ポーラー（または中央確認）に一本化**し二重バンプを防ぐ。
4. **P4** メトリクス POST に実値を載せる。
5. **P5/P6** は整合性・可観測性の改善。

---

## 既知の関連ファイル（調査・修正の起点）

- `Serverside_HFL/edge_server/endpoints/terminal_update.py`
- `Serverside_HFL/central_server/endpoints/edge_update.py`（push_to_edge トリガ周辺）
- `Serverside_HFL/central_server/endpoints/edge_management.py`（`async_send_model_and_app` 等）
- `Serverside_HFL/edge_server/` 内 `receive_model` 定義箇所（grep 推奨）
- `HFL_terminals/.../AppConfig.kt`（SSID → ポート）
- `HFL_terminals/.../training/TrainingViewModel.kt`（`virtual_router_id`）

以上。
