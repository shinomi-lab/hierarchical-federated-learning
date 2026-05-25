# 接続先変更（エッジ/ルータ）対応完了報告書

サーバー側仕様書「端末仕様書: 接続先変更（エッジ/ルータ）とラウンド整合性」に基づき、以下の実装を完了しました。

## 1. 実装完了項目

### A. 接続先ID管理機能 (`AppConfig.kt`)
*   端末ローカルの `client_config.json` から以下のIDを読み込む機能を追加しました。
    *   `current_edge_id`: 現在のエッジID
    *   `target_edge_id`: 次ラウン��の推奨エッジID
    *   `current_router_id`: 現在のルータID
    *   `target_router_id`: 次ラウンドの推奨ルータID
*   ファイルが存在しない場合はデフォルト値を使用します。

### B. 必須メタデータの送信 (`TrainingViewModel.kt`)
*   モデル更新アップロード時の `client_meta` (JSON) に、仕様書で定義された以下の必須フィールドを追加しました。
    *   `current_edge_id` / `target_edge_id`
    *   `current_router_id` / `target_router_id`
    *   `change_effective_round`: 現在のラウンド + 1 を自動設定
    *   `change_announced_at`: 現在時刻 (ISO8601)
    *   `reqId`: UUIDによるリクエストID

## 2. 運用・確認手順

### 端末設定 (`client_config.json`)
接続先を変更したい場合、端末の `filesDir` (例: `/data/data/com.example.hfl_experiment/files/`) にある `client_config.json` を以下のように更新してください。

```json
{
  "terminal_id": "device-001",
  "current_edge_id": "edge-A",
  "target_edge_id": "edge-B",
  "current_router_id": "router-X",
  "target_router_id": "router-Y"
}
```

### サーバー側での確認
*   端末から送信された `client_meta` を確認し、`target_edge_id` が意図通り設定されているか確認してください。
*   `change_effective_round` が正しく「次ラウンド」になっているか確認してください。

## 3. 補足
*   本実装は「メタデータの送信」までをカバーしています。実際のネットワーク切替（Wi-Fi接続先の変更など）は、別途 `NetworkSwitchHelper` 等を用いて行うか、運用スクリプトで制御する必要があります。
*   409 Conflict (Round Mismatch) エラー時の自動リトライは既存実装でカバーされています。

以上で、サーバー側仕様書に対する端末側の対応は完了です。

