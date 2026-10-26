# AP切り替え推論スナップショットの挙動に関する調査報告

**作成日:** 2026-04-27
**対象コンポーネント:** `Serverside_HFL/start.py` (Tabキー等で呼び出される `_do_inference_snapshot` 処理)

## 1. 問題の事象
サーバー起動中（`start.py` 実行中）に Tab キーを押して呼び出す「AP 切り替え推論スナップショット」画面において、以下の現象が発生している。
*   `⚠ research_log が見つかりません。学習が開始されていないか、端末が接続していません。` という警告が表示される。
*   端末IDが `Fallback` となり、「TP実測」と「RTT実測」が `5Mbps / 0ms` や `10Mbps / 200ms` のような固定値（静的な要件値）として表示され、実際の端末の通信状況が反映されていない。

## 2. 原因の特定

ソースコード（`Serverside_HFL/start.py` の 866行目付近）を調査した結果、以下のロジックが原因であることが判明した。

### 2-1. `research_log` ディレクトリへのハードコードされた依存
スナップショットの描画処理は、各端末の最新の通信実測値（TP / RTT / 使用中のアプリ）を取得するために、以下の特定ディレクトリ内の JSONL ファイルをスキャンしようとしている。
*   **パス:** `Serverside_HFL/received_files/research_log/research_*.jsonl`

しかし、現在の最新のエッジサーバー（`edge_server/endpoints`）の実装および、Android端末側（`HFL_terminals`）の実装には、**この形式でログをアップロード・保存する機能が存在しない**（過去の実験構成の名残、または別のダッシュボード用システムと想定される）。

### 2-2. 取得失敗時の「Fallback（フォールバック）」ロジックの作動
上記のディレクトリにファイルが見つからず、端末のリスト（`terminal_data`）が空（Empty）となった場合、スナップショット機能は**「Fallback（代替）」モード**に移行する。

このモードでは、サーバー設定ファイル（`app.json` 等）に定義されている**「各アプリが理想的に要求する通信品質（needTP / needRTT）」を、あたかも実測値であるかのようにモデルに入力**してダミーの推論を実行し、画面に表示する。
*   ブラウザ: needTP = 5 Mbps, needRTT = 0 ms
*   動画: needTP = 10 Mbps, needRTT = 0 ms
*   通話: needTP = 0 Mbps, needRTT = 200 ms

これが、スナップショット画面に常に同じ「5Mbps」や「0ms」が表示されていた理由である。

## 3. 結論と今後の方針

現在の推論スナップショット画面が「変」に見えるのは、**スナップショット機能が参照しようとしているログの場所と、現在のシステムが実際にメトリクス（Telemetry）を保存している場所が食い違っている**ためである。

**（参考）現在の正しいログの場所:**
*   現在のシステムでは、端末からのメトリクスや満足度は `logs/time_records/central_server_*.log` や、SQLite データベース、または `telemetry` 受信用の `terminal_satisfaction.json` などに統合されている。

※ 本調査は原因の特定のみを目的としており、ソースコードの修正は行っていない。
もし今後、このスナップショット画面を「現在接続している実機の生のメトリクス」で正しく描画させたい場合は、`start.py` の `_do_inference_snapshot` 関数が、最新の `time_records` ログまたは `metrics` データベースを参照するように改修する必要がある。

## 4. 直近の変更方針（2026-04-29）

現状の `Fallback` 直接原因は、`start.py` が参照している `training_metrics_received` に `terminal_id` が入っていないこと。
ログ上は `training_metrics_received` 自体は存在するが、`terminal_id=null` のため、端末別表示に使う `terminal_data` が空扱いになる。

### 4-1. 方針A（最優先）: エッジ→中央転送で `terminal_id` を必ず送る

- 対象: `Serverside_HFL/edge_server/main.py` の `send_training_metrics_to_central_server()`
- 変更:
  - 関数引数に `terminal_id` を追加
  - 中央送信用 `data` に `terminal_id` を追加
- 呼び出し元:
  - `Serverside_HFL/edge_server/endpoints/terminal_update.py` から `terminal_id=terminal_id` を渡す

期待効果:
- 中央ログ `training_metrics_received` に端末IDが入り、`_do_inference_snapshot` が `Fallback` ではなく端末行を表示できる。

### 4-2. 方針B（同時対応推奨）: TP/RTT 実測値も転送する

- 対象: 同上 (`send_training_metrics_to_central_server`)
- 変更:
  - `tp_measured_mbps` / `rtt_measured_ms` を任意引数として追加し、中央へ送信
  - `terminal_update.py` 側から取得済み値を渡す

期待効果:
- スナップショットの「TP実測 / RTT実測」が静的代替値ではなく、端末報告値ベースで表示しやすくなる。

### 4-3. 方針C（安全策）: `start.py` の参照先フォールバック強化

- 対象: `Serverside_HFL/start.py` `_do_inference_snapshot`
- 変更:
  - `training_metrics_received` から `terminal_id` が取れない場合、`edge time_records` の `telemetry_received` / `weights_received` も補助参照
  - データ欠落時メッセージに「terminal_id 欠落の可能性」を追記

期待効果:
- 一部経路の転送漏れがあっても、オペレーション時の可観測性を確保できる。

### 4-4. 検証観点

1. 端末学習後、中央ログ `training_metrics_received` に `terminal_id` が非nullで出ること  
2. `start.py` スナップショットで `Fallback` 行ではなく `terminal-xx` 行が出ること  
3. 可能なら `TP実測 / RTT実測` 列に端末起因の値が反映されること