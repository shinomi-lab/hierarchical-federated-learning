# 実機 HFL 実験 — 学習関連パラメータ一覧

> **参照ファイル**
> - `HFL_terminals/app/src/main/java/com/example/hfl_experiment/training/core/LocalTrainer.kt`
> - `HFL_terminals/app/src/main/java/com/example/hfl_experiment/training/TrainingViewModel.kt`
> - `HFL_terminals/app/src/main/java/com/example/hfl_experiment/experiment/DecisionEngine.kt`
> - `HFL_terminals/app/src/main/java/com/example/hfl_experiment/util/TerminalSatisfaction.kt`
> - `HFL_terminals/app/src/main/assets/ap_config.json`
> - `Serverside_HFL/edge_server/endpoints/virtual_congestion.py`

---

## 1. モデルアーキテクチャ

| パラメータ | 値 | 実機コード定義箇所 |
|---|---|---|
| `inputSize` | **6** | `tpRttFeatures(2) + APP_CAT_COUNT(4)` |
| `hiddenSize` | **32** | `TrainingViewModel.kt:918` `modelHiddenSize = 32` |
| `outputSize` | **4** | `AppConfig.APP_CAT_COUNT = 4` |
| `dropoutP` | **0.1** | `TrainingViewModel.kt:1157` |

### ネットワーク構造

```
入力 (6次元: [TP, RTT, browser, video, call, other])
  ↓
Linear(6 → 32)
  ↓
LayerNorm(32)  ← gamma / beta あり
  ↓
ReLU
  ↓
Dropout(p=0.1)
  ↓
Linear(32 → 32)
  ↓
LayerNorm(32)
  ↓
ReLU
  ↓
Dropout(p=0.1)
  ↓
Linear(32 → 4)
  ↓
出力 (4クラス: 満足度ティア 0〜3)
```

### 入力特徴量の定義

| インデックス | 名前 | 意味 | 正規化 |
|---|---|---|---|
| 0 | `tp_norm` | スループット | `min(tp / TP_MAX, 1.0)` |
| 1 | `rtt_norm` | 遅延 | `max(1.0 - rtt / RTT_MAX, 0.0)` |
| 2 | `app_browser` | ブラウザ one-hot | 0 or 1 |
| 3 | `app_video` | 動画 one-hot | 0 or 1 |
| 4 | `app_call` | 通話 one-hot | 0 or 1 |
| 5 | `app_other` | その他 one-hot | 0 or 1 |

---

## 2. 学習ハイパーパラメータ

| パラメータ | 値 | 実機コード定義箇所 |
|---|---|---|
| `learningRate` | **0.0001** | `TrainingViewModel.kt:1158` |
| `weightDecay` | **0.01** | `TrainingViewModel.kt:1159` |
| `localBatchSize` | **16** | `TrainingViewModel.kt:1161` |
| `localEpochs` | **5** | `TrainingViewModel.kt:1160`（epochs引数で渡される） |
| `clipMaxNorm` | **1.0** | `TrainingViewModel.kt:1162` |
| `warmupSteps` | **10** | `TrainingViewModel.kt:1163` |
| `minLrRatio` | **0.1** | `TrainingViewModel.kt:1165` |
| `totalSteps` | `(dataset.size / 16) × epochs` | `TrainingViewModel.kt:1164` |

### オプティマイザ：AdamW

| パラメータ | 値 | 備考 |
|---|---|---|
| `β1` | **0.9** | `LocalTrainer.kt:463` |
| `β2` | **0.999** | `LocalTrainer.kt:464` |
| `ε` | **1e-8** | `LocalTrainer.kt:465` |
| LayerNorm の weightDecay | **0.0** | `isNormParam=true` の場合は weight decay を適用しない |

### LRスケジューラ：LinearWarmup

```
step < warmupSteps  → lr = baseLr × (step / warmupSteps)
step > totalSteps   → lr = baseLr × minLrRatio
それ以外           → 線形減衰
  decayRatio = (step - warmupSteps) / (totalSteps - warmupSteps)
  lr = baseLr × ((1 - minLrRatio) × (1 - decayRatio) + minLrRatio)
```

> **注意**: `scheduler.step()` は `optimizer.step()` の **後** に呼ばれる（step は 1 始まり）

### 損失関数：CrossEntropy

```
loss = (1/B) × Σ [ -logit[target] + log(Σ exp(logit[j])) ]
```

---

## 3. データ分割

| 分割 | 割合 | 実機コード |
|---|---|---|
| 訓練 (train) | **80%** | `(n × 0.8f).toInt()` |
| 検証 (val) | **10%** | `(n × 0.1f).toInt()` |
| テスト (test) | **10%** | `n - trainCount - valCount` |

- 分割は**先頭から順番**に割り当て（`subList`）、random_split は使用しない
- バッチシャッフルは**訓練開始時に1度だけ**実施し、全エポックで同じ順序を維持（`generateBatches(shuffle=true)`）

---

## 4. 実験フロー（1回の試行）

```
runFiveCycles() が呼ばれる
│
├─ 満足度を計測 (satisfaction_before)
│
├─ for cycle in 1..5:                    ← ローカルラウンド × 5
│     performLocalTrainingSuspend(epochs=5)  ← 5 エポック学習
│     (学習後に重みをエッジサーバーへアップロード)
│     (エッジは閾値台数に達したら集約 → セントラルへ送信)
│     (セントラルは全エッジ集約後にグローバルモデルを配布)
│
├─ 推論 (DecisionEngine.observe)
│     → AP選択 (維持 or 変更)
│
└─ 満足度を計測 (satisfaction_after)
      → 1 グローバルラウンド完了
```

| フロー項目 | 値 |
|---|---|
| グローバルラウンド数 | **30回** |
| ローカルラウンド数（1試行あたり） | **5回** |
| エポック数（1ローカルラウンドあたり） | **5エポック** |
| 1試行あたりの総エポック数 | **25エポック**（5×5） |

---

## 5. モデルパラメータの直列化（f32_flat形式）

エッジサーバーへの送信・受信に使用するバイナリフォーマット（リトルエンディアン float32）

| 順序 | パラメータ | サイズ |
|---|---|---|
| 1 | `layer1.weight` | 32 × 6 = 192 |
| 2 | `layer1.bias` | 32 |
| 3 | `norm1.gamma` (LayerNorm γ) | 32 |
| 4 | `norm1.beta` (LayerNorm β) | 32 |
| 5 | `layer2.weight` | 32 × 32 = 1024 |
| 6 | `layer2.bias` | 32 |
| 7 | `norm2.gamma` | 32 |
| 8 | `norm2.beta` | 32 |
| 9 | `layer3.weight` | 4 × 32 = 128 |
| 10 | `layer3.bias` | 4 |
| **合計** | | **1540 floats = 6160 bytes** |

---

## 6. 端末満足度（TerminalSatisfaction）

### 計算式

| アプリ種別 | 優先指標 | 計算式 |
|---|---|---|
| `browser` | TP優先 | `S = TP_link / TP_need` |
| `video` | TP優先 | `S = TP_link / TP_need` |
| `call` | RTT優先 | `S = RTT_need / RTT_link` |
| `other` | TP優先 | `S = TP_link / TP_need` |

- **クリップ**: `S ∈ (0, 1]`（0以下は EPS=1e-6 に丸める、1超えは1に丸める）

### アプリ別 QoS 要件（ap_config.json）

| アプリ | インデックス | needTP (Mbps) | needRTT (ms) | 優先指標 |
|---|---|---|---|---|
| `browser` | app0 | **5.0** | 100 | TP |
| `video` | app1 | **2.0** | 50 | TP |
| `call` | app2 | 10.0 | **200** | RTT |
| `other` | app3 | **1.0** | 30 | TP |

---

## 7. 意思決定エンジン（DecisionEngine）

### 設定値（ap_config.json）

| パラメータ | 値 | 意味 |
|---|---|---|
| `emaAlpha` | **0.6** | EMAの平滑化係数（新しい値の重み） |
| `consecutiveThreshold` | **3** | 連続してベストAPと判定された回数の閾値 |
| `minSwitchSeconds` | **300** | AP切替後の最小待機時間（秒） |
| `deltaThreshold` | **0.15** | AP切替を許可するEMAスコア差の最小値 |
| `cycleWindow` | **5** | 評価を行う観測ウィンドウ幅 |

### AP選択ロジック

```
入力: [TP(Mbps), RTT(ms), appOneHot(4D)] → モデル推論 → rawScores
         ↓
満足度補正: corrected[i] = rawScores[i] × satisfaction(appType, TP, RTT)
         ↓
EMA更新: ema[i] = ema[i] × (1 - α) + corrected[i] × α
         ↓
cycleWindow 回ごとに評価:
  best = argmax(ema)
  │
  ├─ 強制クールダウン中 / minSwitchSeconds 未経過 → 現在APを維持
  ├─ ema[best] - ema[2nd] < deltaThreshold        → 連続カウントリセット・維持
  ├─ consecutiveWins[best] < consecutiveThreshold  → カウント++・維持
  └─ consecutiveWins[best] ≥ consecutiveThreshold  → AP切替実行
```

---

## 8. ネットワーク条件（virtual_congestion.py）

実機実験では、エッジサーバーが端末に対してネットワーク遅延を動的に注入する。

### APパラメータ（ROUTER_PARAMS）

| AP | `initial_rtt_ms` | `tp_init_mbps` | `mu` (サービス率) | `n` (チャネル数) |
|---|---|---|---|---|
| **AP_A** (WiFi) | 20.0 ms | 50.0 Mbps | 2.0 | 2 |
| **AP_B** (Cellular) | 80.0 ms | 20.0 Mbps | 5.0 | 10 |

### RTT計算式（M/M/1モデル）

```
λ = 接続端末数
virtual_rtt = 1 / (μ - λ) × 1000  [ms]  (λ ≥ μ の場合は 10000ms)
rtt_report  = initial_rtt_ms + max(0, virtual_rtt - initial_rtt_ms)
```

### TP計算式（Erlang-Bモデル）

```
A = λ / μ  (offered load)
p_loss = erlang_b(n, A)
TP_actual = tp_init × (1 - p_loss)  [Mbps]
```

### 接続台数ごとの実効値（現在のパラメータ）

**AP_A (WiFi)**

| 接続台数 | RTT (ms) | TP (Mbps) | call満足度 |
|---|---|---|---|
| 0 | 500 | ~50.0 | 0.40 (tier1) |
| 1 | 1000 | ~46.2 | 0.20 (tier0) |
| 2 | 飽和 | ~40.0 | 0.02 (tier0) |

**AP_B (Cellular)**

| 接続台数 | RTT (ms) | TP (Mbps) | call満足度 |
|---|---|---|---|
| 0 | 200 | ~20.0 | 1.00 (tier3) |
| 2 | 333 | ~20.0 | 0.60 (tier2) |
| 3 | 500 | ~20.0 | 0.40 (tier1) |
| 4 | 1000 | ~20.0 | 0.20 (tier0) |

---

## 9. シミュレーション（Sim_HFL）との対応

| 項目 | 実機コード | シミュレーションコード |
|---|---|---|
| モデル定義 | `LocalTrainer.kt :: MLPModel` | `sim/model.py :: APSelectionMLP` |
| 学習ループ | `LocalTrainer.kt :: updateWeightsAndGetLoss` | `sim/terminal.py :: SimTerminal.local_train` |
| LRスケジューラ | `LocalTrainer.kt :: LinearWarmupScheduler` | `sim/terminal.py :: _kotlin_lr` |
| 満足度計算 | `TerminalSatisfaction.kt :: calculateSatisfaction` | `sim/data_gen.py :: terminal_satisfaction` |
| AP選択 | `DecisionEngine.kt :: observe` | `sim/terminal.py :: run_inference` |
| ネットワーク条件 | `virtual_congestion.py :: ROUTER_PARAMS` | `config/default.yaml :: ap_a / ap_b` |
| QoS要件 | `ap_config.json :: needTP / needRTT` | `sim/data_gen.py :: _TP_NEEDS / _RTT_NEEDS` |
| f32_flat直列化 | `LocalTrainer.kt :: loadFromFlat` | `sim/model.py :: from_f32_flat / to_f32_flat` |
