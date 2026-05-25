# 端末クライアント動作仕様およびデータ形式（統合版）

本仕様書は、Android端末クライアントにおける学習サイクルの制御フローおよび、サーバ送信データ（メタ/学習指標）の詳細仕様に加え、ラウンド境界での接続先（エッジ）・ルータ切替要件を定義します。共有ストレージを前提に、サーバ側はイベント集約で変更を検知・監視します。

## 目的
- ラウンド整合性の維持（同ラウンド内の接続先は固定）
- 手動切替の制約下でも変更意図・適用状況を可視化
- バージョンずれ・レース条件の回避

---

## 1. 学習サイクルの制御フロー

端末は1回の実行指示につき、規定のサイクル数（デフォルト: 5サイクル）の学習を行います。各サイクル終了後、次のサイクルへ進む前に以下の手順で同期制御を行い、学習処理を一時停止します（ラウンド境界での整合性確保）。

1. ローカル学習 (Local Training)
  - 指定エポック数の学習を実行。
2. 結果アップロード (Upload)
  - 学習済みモデルパラメータ（重み）をサーバへ送信（`client_meta` を含む）。
3. ラウンド進行待機 (Wait for Round Advance)
  - サーバのメタデータAPI (`/api/v1/meta`) をポーリングし、サーバ側のラウンド番号 (`round`) が進むまで待機。
  - この間、学習処理は停止。
4. 新モデル受信待機 (Wait for New Model)
  - サーバからのモデル配布通知（またはポーリング）を待機。新モデルのダウンロードと適用が完了するまで次サイクルは開始しない。
5. 次サイクル開始
  - 受信したグローバルモデルを初期値として、次のローカル学習を開始。

## 用語
- ラウンド: 学習更新の一単位。端末は各ラウンドでモデル更新を送信します。
- エッジID (`edge_id`): 端末が接続するエッジサーバの識別子。
- ルータID (`router_id`): 端末が利用するネットワーク経路（ルータ/アクセスポイントなど）の識別子（任意）。

## サーバ（エッジ）とルータの明確な区別
- サーバ（エッジ）: 学習更新を受理・検証・集約する「処理の行き先」。識別子は `edge_id`。
- ルータ（ネットワーク経路）: パケットの経路品質（帯域/遅延/安定性）に関与する「通信の道」。識別子は `router_id`（任意）。

なぜ `current_edge_id`/`target_edge_id` をメタに含めるのか:
- ラウンド整合性の検証に必須（意図した接続先と現在の接続先の一致を確認）。
- 誤配送/重複送信の抑止（同ラウンドで複数エッジ送信を検知・デデュープ）。
- 集約・可視化で切替進捗を正しく計測（どのエッジに送ったかを横断集計）。

注意（ミスリード防止）:
- ルータ切替はネットワーク経路の変更であり、接続先サーバ（エッジ）の変更とは別概念です。`router_id` は任意項目で、サーバの検証ロジックは `edge_id` を基準に動作します。

## 2. 送信データ仕様

### A. モデルアップロード時のメタデータ (`client_meta`)

重みデータ送信時、マルチパートリクエストの `client_meta` フィールド（JSON文字列）として以下を付与します。

【基本情報】
- `terminal_id` (string): 端末識別子（例: `device-001`）
- `round` (int): 参加ラウンド番号
- `model_id` (string): 学習のベースとなったモデルID
- `timestamp_ms` (long): 送信時のUNIXミリ秒

【学習指標】
- `total_local_ms` (long): ローカル学習合計時間
- `epoch_durations_ms` (long[]): 各エポック所要時間
- `last_train_cpu_ms` (long): 学習プロセスCPU時間

【デバイス状態】
- `battery_before_pct`/`battery_after_pct` (int): 学習前後のバッテリー残量(%)
- `heap_before_bytes`/`heap_after_bytes` (long): 学習前後のヒープメモリ使用量
- `device_model` (string): 端末モデル名（例: `Pixel 6`）
- `android_sdk_int` (int): Android SDKバージョン

【ネットワーク】
- `network_transport` (string): 通信種別（`WIFI`/`CELLULAR`/`ETHERNET` 等）
- `wifi_ssid` (string): Wi-Fi SSID（権限がある場合）
- `wifi_link_speed_mbps` (int): Wi-Fiリンク速度(Mbps)
- `wifi_rssi_dbm` (int): Wi-Fi電波強度(dBm)

【データ分布】
- `app_distribution` (object): 学習データのラベル分布（例: `{"0":25,"1":25}`）

【接続先/切替（整合性用）】
- `current_edge_id` (string): 現在接続中のエッジID
- `target_edge_id` (string): 次ラウンドから接続したいエッジID
- `change_effective_round` (int): 切替有効ラウンド番号（通常は `current_round+1`）
- `change_announced_at` (string, ISO8601, 任意): 変更意図決定時刻
- `current_router_id` / `target_router_id` (string, 任意): 現在/次ラウンドで利用するルータ/経路の識別子
- `reqId` (string, 任意): リクエストID（重複検知）

### B. 学習指標データ (Metrics)

学習進捗として、以下のデータを送信（またはログ収集）します。

- `epoch` (int): エポック番号
- `loss` (float): 損失値
- `accuracy` (float): 正答率（0.0–1.0）
- `duration_ms` (long): 当該エポック所要時間

## 3. 切替ルール（厳守）
- ラウンド境界でのみ切替可能。
  - 現在ラウンド中は `current_edge_id` 固定（スティッキー）。同ラウンドで複数エッジ送信は禁止。
  - `change_effective_round` は「次ラウンド番号」を指定。
  - ルータ切替（`current_router_id/target_router_id`）もラウンド境界でのみ適用。途中の経路変更は避け、ネットワーク不安定時は次ラウンドへ遅延。
- 事前ハンドシェイク（推奨）
  - 新接続先の `/api/v1/meta` を呼び出し、モデルID・ラウンドの合意を確認。
  - 合意不可の場合は切替を次ラウンドに遅延し、旧接続先に継続送信。

## 4. エラーハンドリング
- 409 Conflict (Round Mismatch)
  - アップロード時に `409` が返された場合、最新メタを再取得し、正しいラウンド情報で再送。
  - `change_effective_round` とサーバ現在ラウンドの不一致が原因の場合、次ラウンドで適用。
- 403 Forbidden
  - 新接続先の許可リスト未登録。運用側に反映依頼。
  - グレース期間設定がある場合は旧接続先へ1ラウンドのみフォールバック可（運用設定）。
- 413 Payload Too Large
  - ペイロードサイズ超過。送信内容の縮小や分割で再送。
- 通信エラー
  - 指数バックオフでリトライ。永続的失敗はイベント記録し次サイクルで再試行。

## 5. 重複送信の扱い
- サーバ側は `sha256 + terminal_id + round` をキーとして重複をデデュープ。
- 端末側も同ラウンド内の複数送信は避ける。

## 6. 許可リスト運用
- 新接続先へ切替する前に、運用側でエッジの許可リストへ端末IDを登録。
- エッジは共有ストレージの許可リストJSONを周期的（例: 60秒）に再読込。

## 7. 推奨ワークフロー（端末側）
1. 現在ラウンド終了。
2. サーバ推奨に基づき `target_edge_id` を更新。
3. 新接続先 `/api/v1/meta` で合意確認（モデルID・ラウンド）。
4. `change_effective_round = 次ラウンド番号` を設定。
5. 次ラウンド開始時に `current_edge_id = target_edge_id` に更新。
6. 新接続先へモデル更新を送信。

## 8. 監視と可視化（サーバ側）
- イベント
  - `connection_change_announced`: `current_edge_id != target_edge_id` の受信時記録。
  - `connection_change_applied`: `current_edge_id == EDGE_SERVER_ID` かつ `change_effective_round == server_round` の適用時記録。
  - `router_change_announced/applied`: `current_router_id != target_router_id` の受信/適用時記録（任意）。
- 集約
  - `tools/shared_storage_rollup.py --latest` で横断集計し、切替意図件数／未反映端末数／409件数などを `logs/analysis` に出力。

## 9. 具体例
- 例1: ルータのみ切替（`router_id` 変更、`edge_id` は据え置き）
  - ネットワーク品質改善を狙うが、学習更新の送信先は同じエッジ。サーバ検証は成功し、ラウンド整合性維持。
- 例2: エッジを切替（`edge_id` 変更、`router_id` は任意）
  - 次ラウンド開始で `current_edge_id = target_edge_id` に更新。旧エッジへの送信は停止。409や403を回避するため許可リスト事前同期とメタ合意確認を実施。

## 10. 互換性
- 既存端末は `client_meta` に上記キーを追加するだけで移行可能。
- サーバ側は不一致をイベント記録し、グレース期間（1ラウンド）で旧接続先の受理を許可可能（運用設定による）。

## 11. セキュリティ
- 認可トークン（Bearer）を用いる場合、メタ送信時も同様に付与してください。
- 許可リストは共有ストレージ管理下で更新され、周期再読込により反映されます。

## 12. 例（メタ断片）
```json
{
  "client_meta": {
    "current_edge_id": "edge-A",
    "target_edge_id": "edge-B",
    "change_effective_round": 31,
    "change_announced_at": "2025-12-14T09:12:00Z",
    "reqId": "abc123",
    "current_router_id": "router-X",
    "target_router_id": "router-Y"
  }
}
```

## 13. よくある質問
- Q: ラウンド中に切替したい場合は？
  - A: 禁止です。境界のみ許可してください。409や不整合の原因になります。
- Q: 許可リスト未反映で403が出ます。
  - A: 運用側に反映を依頼し、次ラウンドで切替してください。グレース期間設定がある場合は旧接続先に一時フォールバック可能です。

---
本仕様は運用の負荷を増やさず、ラウンド整合性と変更検知を最大化するための最小要件を定義しています。

## 14. 5サイクル終了後のテレメトリ送信（端末側拡張）
- 目的: サイクル完了時点の通信品質とアプリ要求に基づく満足度を可視化し、次ラウンドでの経路/接続先検討材料とする。
- 送信タイミング: クライアントが連続5サイクルの学習処理を完了した直後（次サイクル開始前）。
- ペイロード例（JSON）:
  ```json
  {
    "event": "run_completed",
    "satisfaction": 0.73,
    "link_value": 85.0,
    "need_value": 100.0,
    "app_group": "G_TP",
    "terminal_id": "device-001",
    "timestamp_ms": 1760000000123
  }
  ```
- キー定義:
  - `event`: 固定で `run_completed`。5サイクル完了のマーカー。
  - `satisfaction`: $\text{satisfaction} \in [0,1]$ の満足度（リンク品質と要求値の関数）。
  - `link_value`: 計測通信品質指標（例: TPまたはRTTに基づくスコア）。
  - `need_value`: アプリ要求品質（グループ毎の基準値）。
  - `app_group`: アプリケーショングループ（`G_TP` または `G_RTT`）。
  - `terminal_id`: 端末識別子。
  - `timestamp_ms`: UNIXミリ秒。
- サーバ側の取り扱い（推奨）:
  - 受信イベント名: `run_completed` をそのまま `logs/events/*.jsonl` に記録。
  - 集約: `tools/shared_storage_rollup.py --latest` にて `run_completed` を取り込み、満足度の分布、しきい値未満件数、グループ別統計を `logs/analysis` に出力。
  - 閾値判定: `satisfaction < 0.8` を暫定閾値とし、`connection_change_announced` と併せてダッシュボードで可視化（運用ポリシーに応じて変更可）。
  - エンドポイント: 既存のテレメトリ受信APIが無い場合は `POST /api/v1/telemetry` を用意し、上記ペイロードを受理・記録する。
