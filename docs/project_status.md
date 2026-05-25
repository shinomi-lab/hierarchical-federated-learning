# HFL プロジェクト 現状ドキュメント

> 作成日: 2026-04-12  
> 対象: `/Users/tetsuya/HFL/` 以下の全コンポーネント

---

## 1. プロジェクト概要

**階層型連合学習（Hierarchical Federated Learning: HFL）を用いた、アプリ種別に応じたアクセスポイント（AP）自動選択システム**の研究プロジェクト。

Android 端末がアプリ別の QoS（TP/RTT）要件に基づいて WiFi または セルラーを動的に選択し、その推論モデルを端末・エッジ・セントラルの3層でフェデレーテッド学習する。

### 研究設計（実機実験）

| 項目 | 値 |
|---|---|
| 端末数 | 3台（物理 Android）|
| エッジサーバー数 | 2台 |
| トポロジー | エッジ1 ← 端末0, 端末1 ／ エッジ2 ← 端末2 |
| AP 種類 | AP_A（WiFi: 高TP・高RTT）/ AP_B（セルラー: 低TP・低RTT）|
| 実験ラウンド | 30グローバルラウンド |
| ローカルラウンド/グローバルラウンド | 5回 |
| エポック/ローカルラウンド | 5エポック |
| 1グローバルラウンドの総エポック数 | 25エポック |

---

## 2. ディレクトリ構成

```
/Users/tetsuya/HFL/
├── HFL_terminals/          # Android 端末アプリ（Kotlin / Jetpack Compose）
├── Serverside_HFL/         # Python サーバー（FastAPI）
│   ├── edge_server/        # エッジサーバー（ポート 8001）
│   └── central_server/     # セントラルサーバー（ポート 8000）
├── Sim_HFL/                # Pythonシミュレーション環境
│   ├── sim/                # シミュレーションコア
│   ├── analysis/           # 結果分析・グラフ生成
│   ├── config/             # 設定ファイル
│   ├── results/            # 実験結果（68件のレポートMD）
│   └── theory/             # FedAvg 収束理論
├── terminal_client/        # Docker仮想端末クライアント（Python）
├── docker/                 # Docker Compose・各 Dockerfile・README
├── compose.yaml            # ルートから `docker compose up` 用（docker/ を include）
├── docs/                   # プロジェクトドキュメント
│   ├── real_device_parameters.md      # 実機パラメータまとめ
│   ├── research_overview.md           # 研究概要（マクロ〜ミクロ）
│   ├── simulation_analysis_10runs.md  # 10回実験分析レポート
│   ├── research_progress_issues.md    # 発見問題・修正履歴
│   └── project_status.md             # 本ファイル（現状ドキュメント）
└── .cursor/rules/          # Cursor AI ガイドルール（4ファイル）
```

---

## 3. コンポーネント別の現状

### 3-1. HFL_terminals（Android アプリ）

**言語**: Kotlin / Jetpack Compose  
**状態**: コード完成・ビルド済み（デバッグAPK生成済み）

#### 主要ファイル

| ファイル | 役割 | 状態 |
|---|---|---|
| `training/core/LocalTrainer.kt` | ローカル学習エンジン（MLP, AdamW, LinearWarmup） | ✅ 完成 |
| `training/TrainingViewModel.kt` | 実験フロー全体の制御（30ラウンド × 5ローカル × 5エポック） | ✅ 完成 |
| `experiment/DecisionEngine.kt` | AP選択ロジック（EMA・連続閾値・クールダウン） | ✅ 完成 |
| `experiment/ModelLoader.kt` | TorchScript推論エンジン | ✅ 完成 |
| `util/TerminalSatisfaction.kt` | 端末満足度計算（TP優先 / RTT優先） | ✅ 完成 |
| `network/NetworkClient.kt` | HTTP通信（重みアップロード・モデルダウンロード） | ✅ 完成 |
| `util/logging/RealTimeLogger.kt` | JSONL形式のイベントログ | ✅ 完成 |
| `util/networking/PingUtil.kt` | RTT実測（ping） | ✅ 完成 |

#### 実機モデル仕様

| パラメータ | 値 |
|---|---|
| モデル構造 | MLP 3層（input=6 / hidden=32 / output=4）|
| 入力特徴量 | TP_Mbps, RTT_ms, app_browser, app_video, app_call, app_other |
| 出力 | 満足度ティア（0〜3）4クラス分類 |
| 総パラメータ数 | 162（重み+LayerNorm含む）|
| 重み形式 | f32_flat（6160バイト = 1540 floats）|
| Optimizer | AdamW（lr=0.0001, wd=0.01）|
| Scheduler | LinearWarmupDecay（warmup=10, min_ratio=0.1）|
| Gradient Clip | clipMaxNorm=1.0 |
| バッチサイズ | 16 |

---

### 3-2. Serverside_HFL（Python FastAPI サーバー）

**言語**: Python 3.11 / FastAPI / uvicorn  
**状態**: 動作確認済み（ローカル起動可）

#### エッジサーバー（port 8001）

| ファイル | 役割 | 状態 |
|---|---|---|
| `edge_server/endpoints/terminal_update.py` | 端末重みアップロード受信・集約トリガー（1335行） | ✅ 完成 |
| `edge_server/endpoints/aggregation.py` | FedAvg エッジ集約・セントラル送信（1272行） | ✅ 完成 |
| `edge_server/endpoints/virtual_congestion.py` | M/M/1 + Erlang-B ネットワーク遅延注入 | ✅ 完成 |
| `edge_server/endpoints/model_ops.py` | モデル配布（`/send_to_device`）| ✅ 完成 |
| `edge_server/config.py` | 接続設定・閾値（環境プロファイル対応）| ✅ 完成 |
| `shared/event_logger.py` | JSONL構造化ログ共通実装 | ✅ 完成 |

#### セントラルサーバー（port 8000）

| ファイル | 役割 | 状態 |
|---|---|---|
| `central_server/endpoints/edge_update.py` | エッジ集約結果受信・グローバル FedAvg（1152行）| ✅ 完成 |
| `central_server/endpoints/download.py` | グローバルモデル配布 | ✅ 完成 |
| `central_server/config.py` | 接続設定・閾値 | ✅ 完成 |

#### ネットワーク注入パラメータ（`virtual_congestion.py`）

| AP | `tp_init_mbps` | `initial_rtt_ms` | `mu` | `n` |
|---|---|---|---|---|
| AP_A (WiFi) | 50.0 Mbps | 20 ms | 2.0 | 2 チャネル |
| AP_B (セルラー) | 20.0 Mbps | 80 ms | 5.0 | 10 チャネル |

---

### 3-3. Sim_HFL（Pythonシミュレーション）

**言語**: Python 3.11 / PyTorch  
**状態**: 完成・実行済み（68件のレポート生成済み）

#### シミュレーションコアファイル

| ファイル | 行数 | 役割 | 対応実機ファイル |
|---|---|---|---|
| `sim/model.py` | 156行 | MLP モデル定義（実機と完全一致） | `LocalTrainer.kt` |
| `sim/terminal.py` | 501行 | ローカル学習・推論・AP選択 | `TrainingViewModel.kt` + `DecisionEngine.kt` |
| `sim/aggregator.py` | 167行 | FedAvg 集約（NaN/Inf除外） | `aggregation.py` |
| `sim/network_model.py` | 137行 | M/M/1 + Erlang-B 待ち行列モデル | `virtual_congestion.py` |
| `sim/data_gen.py` | 255行 | 合成データ生成（6次元特徴量・4クラスバランス） | `ap_config.json` |
| `sim/runner.py` | 577行 | HFL実験フロー全体制御 | `TrainingViewModel.kt` |
| `sim/display.py` | 335行 | リッチターミナル表示（リアルタイム進捗） | ─ |
| `analysis/report_gen.py` | 609行 | Markdown レポート自動生成 | ─ |
| `analysis/plot_accuracy.py` | 182行 | Accuracy グラフ描画 | ─ |
| `run_sim.py` | 323行 | エントリーポイント（CLIオプション） | ─ |

#### シミュレーション設定（`config/default.yaml`）

```
端末数: 3 / エッジ数: 2 / ラウンド数: 30
ローカルラウンド: 5 / エポック/ローカルラウンド: 5
AP_A: tp=50Mbps, rtt=20ms, mu=2.0, n_ch=2
AP_B: tp=20Mbps, rtt=80ms, mu=5.0, n_ch=10
```

#### 実験結果サマリー（30試行 × 10回実行）

| 指標 | 平均 | 標準偏差 | 最小 | 最大 |
|---|---|---|---|---|
| 最終精度（Round 30） | 81.13% | ±3.57% | 77.00% | 88.67% |
| 収束ラウンド（≥95%最終精度） | 25.7 | ±2.9 | 20 | 29 |
| 全ラウンド平均満足度 | 94.39% | ±9.55% | 70.4% | 100% |

---

### 3-4. Docker 仮想端末環境

**状態**: ファイル完成（未ビルド・未起動）

#### 構成ファイル

| ファイル | 役割 | 状態 |
|---|---|---|
| `docker/Dockerfile.central` | セントラルサーバーイメージ | ✅ 完成 |
| `docker/Dockerfile.edge` | エッジサーバーイメージ | ✅ 完成 |
| `docker/Dockerfile.terminal` | Python仮想端末イメージ | ✅ 完成 |
| `docker/docker-compose.yml` | 全コンテナ一括起動定義 | ✅ 完成 |
| `compose.yaml`（ルート） | `docker/docker-compose.yml` を include | ✅ 完成 |
| `terminal_client/main.py` | 仮想端末ロジック（学習・AP選択・HTTP通信） | ✅ 完成 |
| `terminal_client/requirements.txt` | 依存パッケージ | ✅ 完成 |

#### Docker構成（`docker/docker-compose.yml` 既定）

```
redis × 1
central × 1  (port 8000)  中央集約閾値=2
edge-00 × 1  (port 8001) ← terminal-00, 01（エッジ閾値=2）
edge-01 × 1  (port 8002) ← terminal-02（エッジ閾値=1）
terminal-00〜02 × 3  (仮想端末)
```

より多いエッジ・端末が必要な場合は compose を複製して閾値を合わせて拡張する。

---

## 4. Cursor ルール（AI ガイドライン）

`.cursor/rules/` に4ファイルを設置済み。

| ファイル | 内容 |
|---|---|
| `hfl-architecture.mdc` | プロジェクト全体構造・コンポーネント間の関係 |
| `sim-fidelity.mdc` | シミュレーションと実機の忠実度維持ルール |
| `logging-schema.mdc` | JSONL ログスキーマ・相関ID規則 |
| `config-sync.mdc` | 設定値の同期ルール（ap_config.json ↔ data_gen.py など） |

---

## 5. 実験の実行方法

### シミュレーション実行

```bash
cd /Users/tetsuya/HFL/Sim_HFL
source .venv/bin/activate
# 1回実行
python run_sim.py
# 10回実行（異なるシードで統計取得）
python run_sim.py --runs 10
```

結果は `Sim_HFL/results/reports/<id>_report_<timestamp>.md` に保存される。

### 実機実験の起動手順

```bash
# 1. セントラルサーバー起動（port 8000）
cd /Users/tetsuya/HFL/Serverside_HFL
python -m uvicorn central_server.main:app --host 0.0.0.0 --port 8000

# 2. エッジサーバー起動（port 8001）
python -m uvicorn edge_server.main:app --host 0.0.0.0 --port 8001

# 3. Android 端末に IP アドレスを設定して実験開始ボタンを押す
```

### Docker 環境の起動

```bash
cd /Users/tetsuya/HFL
docker compose up --build
# ログ確認
docker compose logs -f terminal-00
```

---

## 6. 残存する課題（優先度順）

| 優先度 | 課題 | 対象ファイル |
|---|---|---|
| 🔴 高 | f32_flat の最終層キー名不一致（`out.weight` vs `layer3.weight`） | `terminal_update.py` |
| 🔴 高 | 集約閾値のデフォルト値が1（実験では2〜3が必要） | `edge_server/config.py`・`central_server/config.py` |
| 🔴 高 | ベースライン比較なし（AP_B固定・ランダム選択）→ 提案手法の優位性を定量的に示せない | `Sim_HFL/` |
| 🟡 中 | `send_to_device` と `send_to_device_legacy` が並存 | `startup.py`・`model_ops.py` |
| 🟡 中 | `minSwitchSeconds=300`（クールダウン）がシミュレーションに未実装 | `sim/terminal.py` |
| 🟡 中 | 収束に余裕が少ない（平均25.7/30ラウンド） | `config/default.yaml` |
| 🟢 低 | `run_id` が Optional のままで複数実験ログが混在する可能性 | `terminal_update.py` |

---

## 7. 技術スタック一覧

| 領域 | 技術 |
|---|---|
| Android 端末 | Kotlin, Jetpack Compose, PyTorch Android (TorchScript) |
| サーバー | Python 3.11, FastAPI, uvicorn, Redis, httpx |
| シミュレーション | Python 3.11, PyTorch, NumPy, matplotlib, PyYAML |
| Docker | Docker Compose, redis:7-alpine, python:3.11-slim |
| ログ形式 | JSON Lines (JSONL) |
| モデル重み形式 | f32_flat（バイナリ、6160バイト）|
| ネットワークモデル | M/M/1（RTT） + Erlang-B（TP/パケットロス）|
| 学習アルゴリズム | FedAvg（2段階：エッジ集約 → セントラル集約）|

---

## 8. 関連ドキュメント

| ファイル | 内容 |
|---|---|
| `docs/real_device_parameters.md` | 実機学習パラメータ完全一覧 |
| `docs/research_overview.md` | 研究概要（要旨拡張用・マクロ〜ミクロ） |
| `docs/simulation_analysis_10runs.md` | 10回実験の統計分析レポート |
| `docs/research_progress_issues.md` | 発見問題45件・修正38件の履歴 |
| `HFL_terminals/README_experiment.md` | Android側の実験手順 |
| `Serverside_HFL/README.md` | サーバー側の起動手順 |
| `Sim_HFL/README.md` | シミュレーション実行手順 |
