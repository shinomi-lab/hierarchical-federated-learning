# 研究進捗：問題点と修正一覧

> **対象**: Android ↔ サーバー統合、Sim_HFL、実機整合、インフラ  
> **用途**: 研究進捗報告・振り返り用のチェックリスト

## 目次

1. [サマリー](#サマリー)
2. [フェーズ1：Android ↔ サーバー統合](#フェーズ1android--サーバー統合)
3. [フェーズ2：シミュレーション（Sim_HFL）](#フェーズ2シミュレーションsim_hfl)
4. [フェーズ3：実機フローとシミュレーションの整合](#フェーズ3実機フローとシミュレーションの整合)
5. [フェーズ4：環境・インフラ](#フェーズ4環境インフラ)
6. [フェーズ5：残課題・今後対応](#フェーズ5残課題今後対応)
7. [関連ドキュメント](#関連ドキュメント)

---

## サマリー

| フェーズ | 内容 | 問題数 | 修正済 | 残存 |
|----------|------|--------|--------|------|
| 1 | Android ↔ サーバー統合 | 10 | 10 | 0 |
| 2 | Sim_HFL 構築 | 19 | 19 | 0 |
| 3 | 実機フローとの整合 | 6 | 6 | 0 |
| 4 | 環境・インフラ | 3 | 3 | 0 |
| 5 | 未完了・継続課題 | 7 | — | **6**（P5-2 は下記「緩和済み」） |
| **計** | | **45** | **38** | 未対応 **6** |

---

## フェーズ1：Android ↔ サーバー統合

**概要**: エンドポイント・メタデータ・`run_id` / `terminal_id`・メトリクス送信の一貫性を揃えたフェーズ。

### 発見した問題

| ID | 問題点 | 重大度 |
|----|--------|--------|
| P1-1 | `send_to_device` のパスが Android とサーバーで不一致 | 高 |
| P1-2 | `format_version` が整数・文字列で混在 | 高 |
| P1-3 | `run_id` がなく、`round_id` だけでは実験の区別ができない | 高 |
| P1-4 | `terminal_id` が device 名とパッケージ名で混在 | 中 |
| P1-5 | Android が `session_id`、サーバーが `run_id` を期待（キー不一致） | 高 |
| P1-6 | HTTP 4xx/5xx が `RealTimeLogger` に残らない | 中 |
| P1-7 | `persistTrainingMetrics()` が `run_id` 等を `null` のまま送信 | 高 |
| P1-8 | `waitForServerRoundToAdvance` がタイムアウトなしで待ち続けうる | 中 |
| P1-9 | `UploadMetricsWorker.clearAll()` が run を区別せず全削除 | 中 |
| P1-10 | `aggregation.py` が存在しない `UpdateRecord.accuracy` を参照してクラッシュ | 高 |

### 適用した修正

| ID | 修正内容 | 主な対象 |
|----|----------|----------|
| F1-1 | `send_to_device` パスを Android に合わせて統一 | `edge_server/startup.py` |
| F1-2 | `format_version` を整数に統一 | `pt_to_meta_weights.py`・`dist/` meta |
| F1-3 | `run_id`（UUID）を端末・サーバー双方に導入 | `TrainingViewModel.kt`・`terminal_update.py` |
| F1-4 | `terminal_id` を `device_id` に統一 | 複数ファイル |
| F1-5 | `NetworkClient` で `run_id` をフォーム送信 | `NetworkClient.kt` |
| F1-6 | HTTP エラーを `RealTimeLogger` に記録 | `NetworkClient.kt` |
| F1-7 | `persistTrainingMetrics()` でフィールドを正しく渡す | `TrainingViewModel.kt` |
| F1-8 | `waitForServerRoundToAdvance` にタイムアウト追加 | `TrainingViewModel.kt` |
| F1-9 | メトリクス削除を `run_id` 単位に変更 | `UploadMetricsWorker.kt` |
| F1-10 | 精度等を `client_meta` JSON から抽出 | `aggregation.py` |

---

## フェーズ2：シミュレーション（Sim_HFL）

**概要**: 実機 Kotlin と揃えるためのハイパラ・モデル（4クラス満足度ティア）・データ生成・集約バグ修正。

### 発見した問題

| ID | 問題点 | 重大度 |
|----|--------|--------|
| P2-1 | ハイパラが実機と全面不一致 | 高 |
| P2-2 | 出力次元の混乱（実機は満足度4クラス、旧シミュは2クラスAP） | 高 |
| P2-3 | `data_gen` のラベル偏り（約93% browser） | 高 |
| P2-4 | AP設定が両方とも満足度1.0で切替が無意味 | 高 |
| P2-5 | バイアス初期：Kotlin は0、PyTorch は乱数 | 中 |
| P2-6 | データ分割：シミュは random、実機は先頭N件逐次 | 中 |
| P2-7 | `aggregator.py` の NaN 除外後スライスで metrics / `participating_ids` が誤る | 高 |
| P2-8 | `LambdaLR` のウォームアップが期待どおり動かない | 中 |
| P2-9 | LayerNorm に weight_decay がかかっていた | 中 |
| P2-10 | `total_steps` が 0 のまま | 中 |
| P2-11 | パラメータ数が旧式（58）のまま → 実際は162 | 高 |
| P2-12 | `theory_acc = 1 - theory_loss` の誤変換 | 中 |
| P2-13 | 空データ時 `torch.stack([])` でクラッシュ | 中 |
| P2-14 | `compare.py` の相対インポートでトップレベル実行が失敗 | 中 |
| P2-15 | matplotlib 日本語ラベルでフォント警告 | 低 |
| P2-16 | AP選択が旧ラベル設計の残骸（予測アプリと `app_idx` 比較） | 高 |
| P2-17 | JSONL 復元で `num_excluded` 等がなく TypeError | 高 |
| P2-18 | 推論と `best_ap` で別々の TP/RTT サンプル | 中 |
| P2-19 | 満足度の before/after 未分離 | 中 |

### 適用した修正

| ID | 修正内容 | 主な対象 |
|----|----------|----------|
| F2-1 | ハイパラを実機に合わせる（lr, wd, bs, epochs, clip 等） | `config/default.yaml`・`sim/terminal.py` |
| F2-2 | 満足度ティア4クラス・input=6 hidden=32 output=4 | `sim/model.py`・`sim/terminal.py` |
| F2-3 | ティア逆算サンプリングで分布を均等化 | `sim/data_gen.py` |
| F2-4 | WiFi vs セルのコントラストある AP 設定 | `config/default.yaml` |
| F2-5 | バイアスゼロ初期化 | `sim/model.py` |
| F2-6 | 先頭N件の逐次分割 | `sim/terminal.py` |
| F2-7 | `valid_indices` で除外を正確に | `sim/aggregator.py` |
| F2-8 | Kotlin 同等の `LinearWarmupScheduler` | `sim/terminal.py` |
| F2-9 | LayerNorm を `weight_decay=0` の param_group に | `sim/terminal.py` |
| F2-10 | `total_steps` を正しく計算 | `sim/runner.py` |
| F2-11 | パラメータ数を162に修正 | `analysis/compare.py` |
| F2-12 | 理論精度の誤変換をやめ loss のみ比較 | `theory/fedavg_bounds.py` |
| F2-13 | 空データガード | `sim/terminal.py` |
| F2-14 | 絶対インポート化 | `analysis/compare.py`・`run_sim.py` |
| F2-15 | プロットラベルを英語化 | `analysis/plot_accuracy.py` |
| F2-16 | AP選択をティア基準に修正 | `sim/terminal.py` |
| F2-17 | JSONL 復元に欠落フィールドを追加 | `run_sim.py` |
| F2-18 | TP/RTT を同一 RNG で生成し一貫利用 | `sim/runner.py` |
| F2-19 | `satisfaction_before` / `after` を分離 | `sim/terminal.py`・`sim/runner.py` |

---

## フェーズ3：実機フローとシミュレーションの整合

**概要**: 集約タイミング、ネットワークモデル（M/M/1・Erlang-B）、QoS・AP定数・正規化上限の実機／サーバー準拠。

### 発見した問題

| ID | 問題点 | 重大度 |
|----|--------|--------|
| P3-1 | 集約が「30ラウンド最後1回」で実機（各ローカル後）と不一致 | 高 |
| P3-2 | 毎試行初期化で収束しない（設計の混同） | 中 |
| P3-3 | ネットワークが Gaussian のみでサーバ待ち行列と不一致 | 高 |
| P3-4 | needTP/needRTT が実機 `ap_config` と不一致 | 高 |
| P3-5 | シミュ AP 初期値と `virtual_congestion` の `ROUTER_PARAMS` が乖離 | 高 |
| P3-6 | TP 正規化上限がサーバ出力レンジと合わずはみ出し | 中 |

### 適用した修正

| ID | 修正内容 | 主な対象 |
|----|----------|----------|
| F3-1 | 各ローカルラウンド後に中央集約・配布 | `sim/runner.py` |
| F3-2 | モデル引き継ぎ（累積学習）に戻す | `sim/runner.py` |
| F3-3 | `network_model.py` 新規（サーバ式と一致） | `sim/network_model.py` |
| F3-4 | QoS を `ap_config.json` に合わせる | `sim/data_gen.py` |
| F3-5 | AP 初期を `ROUTER_PARAMS` に合わせる | `config/default.yaml` |
| F3-6 | TP_MAX / RTT_MAX を拡張 | `sim/data_gen.py` |

---

## フェーズ4：環境・インフラ

**概要**: パス・スクリプト・Docker 仮想端末の送信形式。

### 発見した問題

| ID | 問題点 | 重大度 |
|----|--------|--------|
| P4-1 | Windows 固有パスがソース・キャッシュ・ドキュメントに残存 | 中 |
| P4-2 | PowerShell のみで Mac から使いにくい | 中 |
| P4-3 | `terminal_client` の f32_flat と HTTP 形式不一致 | 高 |

### 適用した修正

| ID | 修正内容 | 主な対象 |
|----|----------|----------|
| F4-1 | Windows パスをプロジェクト向けに整理 | メタJSON・Kotlin・各種 MD |
| F4-2 | bash 版スクリプト追加 | `scripts/*.sh` |
| F4-3 | `struct.pack` + multipart で f32_flat 送信 | `terminal_client/main.py` |

---

## フェーズ5：残課題・今後対応

### 緩和済み（運用・ランチャー側）

| ID | もともとの問題 | いまの扱い |
|----|----------------|------------|
| **P5-2** | 集約閾値のデフォルトが 1 のまま複数エッジ／端末と合わない | **`Serverside_HFL/start.py`** が環境変数未設定時のみ、中央＝エッジ台数・エッジ2台なら端末閾値 **2,1** を子プロセスに渡す。**`docker/docker-compose.yml`** も中央2・エッジ2／1 に一致。**`uvicorn` だけで中央を起動**する場合は `central_server/config.py` の既定がまだ 1 のため、`CENTRAL_AGGREGATION_THRESHOLD` 等は手動 `export` が必要。 |

### 未対応（優先度の目安）

| ID | 問題点 | 重大度 | 難易度 |
|----|--------|--------|--------|
| P5-1 | f32_flat 解析の最終層キー名（`out.*` vs `layer3`）の食い違い | 高 | 低 |
| P5-3 | `send_to_device` と legacy が並存し誤エンドポイントのリスク | 中 | 中 |
| P5-4 | シミュに `minSwitchSeconds`（300秒）相当のクールダウンがない | 中 | 中 |
| P5-5 | `run_id` 未送信時に `run_unknown` へ混在 | 低 | 低 |
| P5-6 | ベースライン比較（固定AP・ランダム等）が不足 | 高 | 高 |
| P5-7 | 30ラウンドでの収束余裕が小さい（平均約25.7/30） | 中 | 中 |

---

## 関連ドキュメント

| 文書 | 内容 |
|------|------|
| `docs/issue_fix_rationale.md` | **問題のリスクと修正の意図**（説明・中間発表向けの文章資料） |
| `docs/real_device_parameters.md` | 実機パラメータ一覧 |
| `docs/docker_and_logical_clients.md` | Docker・論理クライアント・TP/RTT の整理 |
| `docs/experiment_scale_provisional.md` | 実験規模の暫定方針 |
| `Serverside_HFL/RUN_GUIDE.md` | 起動方法・集約閾値の CLI / 環境変数 |

---

*フェーズ1〜4の P/F 番号は従来ドキュメントとの対応用。P5-2 は `start.py` / Docker で運用面を緩和済み（config 単体既定は別）。*
