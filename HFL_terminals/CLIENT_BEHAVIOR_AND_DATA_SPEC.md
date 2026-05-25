# 端末クライアント動作仕様およびデータ形式

本ドキュメントは、Android端末クライアントにおける学習サイクルの制御フローおよびサーバーへ送信されるデータの詳細仕様をまとめたものです。

## 1. 学習サイクルの制御フロー

端末は1回の実行指示につき、規定のサイクル数（デフォルト: 5サイクル）の学習を行います。各サイクル終了後、次のサイクルへ進む前に以下の手順で同期制御を行い、**学習処理を一時停止**します。

### フロー詳細

1.  **ローカル学習 (Local Training)**
    *   指定エポック数の学習を実行。
2.  **結果アップロード (Upload)**
    *   学習済みモデルパラメータ（重み）をサーバーへ送信。
3.  **ラウンド進行待機 (Wait for Round Advance)**
    *   サーバーのメタデータAPI (`/api/v1/meta`) をポーリングし、サーバー側のラウンド番号 (`round`) が現在のラウンドより進むまで待機。
    *   **この間、学習処理は停止します。**
4.  **新モデル受信待機 (Wait for New Model)**
    *   サーバーからのモデル配布通知（またはポーリングによる検知）を待機。
    *   新モデルのダウンロードおよび適用が完了するまで、次のサイクルは開始されません。
5.  **次サイクル開始**
    *   受信したグローバルモデルを初期値として、次のローカル学習を開始。

---

## 2. 送信データ仕様

端末からサーバーへ送信される主なデータは以下の2種類です。

### A. モデルアップロード時のメタデータ (`client_meta`)

重みデータ送信時、マルチパートリクエストの `client_meta` フィールド（JSON文字列）として以下の情報が付与されます。

| カテゴリ | フィールド名 | 型 | 説明 |
| :--- | :--- | :--- | :--- |
| **基本情報** | `terminal_id` | String | 端末識別子 (例: `device-001`) |
| | `round` | Integer | 参加したラウンド番号 |
| | `model_id` | String | 学習のベースとなったモデルID |
| | `timestamp_ms` | Long | 送信時のUNIXタイムスタンプ (ミリ秒) |
| **学習指標** | `total_local_ms` | Long | ローカル学習にかかった合計時間 (ミリ秒) |
| | `epoch_durations_ms` | Array[Long] | 各エポックの所要時間リスト |
| | `last_train_cpu_ms` | Long | 学習プロセスが消費したCPU時間 (ミリ秒) |
| **デバイス状態** | `battery_before_pct` | Integer | 学習開始時のバッテリー残量 (%) |
| | `battery_after_pct` | Integer | 学習終了時のバッテリー残量 (%) |
| | `heap_before_bytes` | Long | 学習前のヒープメモリ使用量 (Byte) |
| | `heap_after_bytes` | Long | 学習後のヒープメモリ使用量 (Byte) |
| | `device_model` | String | 端末モデル名 (例: `Pixel 6`) |
| | `android_sdk_int` | Integer | Android SDKバージョン |
| **ネットワーク** | `network_transport` | String | 通信種別 (`WIFI`, `CELLULAR`, `ETHERNET` 等) |
| | `wifi_ssid` | String | 接続先Wi-Fi SSID (権限がある場合) |
| | `wifi_link_speed_mbps` | Integer | Wi-Fiリンク速度 (Mbps) |
| | `wifi_rssi_dbm` | Integer | Wi-Fi電波強度 (dBm) |
| **データ分布** | `app_distribution` | Object | 学習データのラベル分布 (例: `{"0": 25, "1": 25}`) |

### B. 学習指標データ (Metrics)

学習の進捗状況として、以下のデータが別途送信（またはログ収集）されます。

| フィールド名 | 型 | 説明 |
| :--- | :--- | :--- |
| `epoch` | Integer | エポック番号 |
| `loss` | Float | 損失値 (Loss) |
| `accuracy` | Float | 正答�� (0.0 - 1.0) |
| `duration_ms` | Long | そのエポックにかかった時間 |

---

## 3. エラーハンドリング

*   **409 Conflict (Round Mismatch)**:
    *   アップロード時にサーバーから `409` が返された場合、端末は自動的に最新のメタデータを再取得し、正しいラウンド情報でリトライを試みます。
*   **通信エラー**:
    *   指数バックオフ（Exponential Backoff）を用いたリトライロジックにより、一時的なネットワーク切断からの復帰を試みます。

