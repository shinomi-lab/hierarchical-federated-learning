# 学習完了後の基地局切り替え推論 — 実装詳細

作成日: 2026-04-23
対象ブランチ: `fully-controled-inference`
主要ファイル: `experiment/DecisionEngine.kt`, `experiment/ModelLoader.kt`, `experiment/Config.kt`

---

## 1. 全体フロー概要

```
FLラウンド完了 → 新モデル配布 → ModelLoader がロード
                                        ↓
                    [観測イベント発生ごとに]
                    DecisionEngine.observe(tp, rtt, appOneHot)
                                        ↓
                    ① MLPモデル フォワードパス  → rawScores[AP-A, AP-B]
                                        ↓
                    ② 満足度補正              → correctedScores[AP-A, AP-B]
                                        ↓
                    ③ EMA平滑化              → emaScores[AP-A, AP-B]
                                        ↓
                    ④ ウィンドウ評価（N観測ごと）
                                        ↓
                    ⑤ 切り替え判断ゲート群
                                        ↓
                    DecisionResult (chosenApId, rawScores, emaScores, reason)
```

---

## 2. 入力データ（observe の引数）

| 引数 | 型 | 内容 | 取得元 |
|------|----|------|--------|
| `tp` | Float | 現在接続中のAPで計測したスループット [Mbps] | `NetworkBandwidthProbe.measureMbps()` |
| `rtt` | Float | 現在接続中のAPへの RTT [ms] | `networkClient.measureRtt()` |
| `appOneHot` | FloatArray(4) | 使用中アプリの one-hot 表現 | 端末に割り当てられたアプリ index |

**重要な制約**: 入力は「現在接続中の AP のみ」の計測値。
**もう一方の AP（非接続側）のリアルタイム品質は取得されていない。**

---

## 3. ステップ①: MLP モデル フォワードパス

```kotlin
// ModelLoader.predict(input: FloatArray): FloatArray
val input = [tp, rtt, appOneHot[0], appOneHot[1], appOneHot[2], appOneHot[3]]  // 6次元

// 正規化: デフォルトなし（weight.bin.norm.json があれば適用）
// モデル構造: Linear(6→32)→LayerNorm→ReLU → Linear(32→32)→LayerNorm→ReLU → Linear(32→4)
// 出力: logits[4] (Softmax なし、生スコア)
```

出力 `rawScores` はクラス 0〜3 のロジット。
有効クラスは **0 (AP-A)** と **1 (AP-B)** の 2 クラス（学習ラベルの値域が {0,1}）。

フォールバック（モデル未ロード時）:
```kotlin
FloatArray(apNum) { idx -> if (idx == 0) tp else tp * 0.9f }
// → TP が高いほど AP-A を優先するヒューリスティック
```

---

## 4. ステップ②: 満足度補正（applySatisfactionCorrection）

モデルのスコアに、アプリ要件を満たせているかの割合を乗算する。

```
correctedScore[i] = rawScore[i] × S

S = TerminalSatisfaction.calculateSatisfaction(appType, tp, rtt, needTP, needRTT)
```

**満足度 S の計算式**（`TerminalSatisfaction.kt`）:

| アプリ種別 | 指標 | 計算式 | クリップ |
|-----------|------|--------|---------|
| browser / video / other | TP優先 | `S = tp_measured / tp_need` | (0, 1] |
| call | RTT優先 | `S = rtt_need / rtt_measured` | (0, 1] |

`ap_config.json` での `needTP` / `needRTT` の実際の値:

| appKey | needTP [Mbps] | needRTT [ms] |
|--------|---------------|--------------|
| app0 (ブラウザ) | 5.0 | 100 |
| app1 (動画) | 2.0 | 50 |
| app2 (通話) | 10.0 | 200 |
| app3 (配信) | 1.0 | 30 |

> ※ `app.json`（サーバ側）と `ap_config.json`（端末側）で needTP/needRTT の値が異なる。
> DecisionEngine は **ap_config.json** の値を使う。

補正の効果：AP の品質がアプリ要件を下回るほど S が小さくなり、そのAPのスコアが抑制される。

---

## 5. ステップ③: EMA 平滑化

毎観測ごとに指数移動平均を更新する。

```kotlin
emaScores[i] = if (emaScores[i] == 0f) corrected[i]
               else emaScores[i] * (1 - alpha) + corrected[i] * alpha
```

- `alpha = emaAlpha = 0.6`（ap_config.json）
- 初回観測は corrected をそのまま代入（EMA 初期化）
- α=0.6 は「直近の観測を 60% 重視」する設定（比較的反応が速い）

---

## 6. ステップ④: ウィンドウ評価

```kotlin
observationCounter = (observationCounter + 1) % cycleWindow  // cycleWindow = 5
if (observationCounter != 0) {
    // ウィンドウ未満 → 切り替え評価せず現状維持
    reason = "pending_window(N/5)"
    return getCurrentPreferred()
}
// ウィンドウ満了（5回に1回）→ 切り替え評価へ
```

5回の観測を 1 ウィンドウとして、ウィンドウ末尾でのみ切り替え評価を実行する。
EMA は毎回更新されるが、AP の変更判断は 5 回に 1 回。

---

## 7. ステップ⑤: 切り替え判断ゲート（4段階）

ウィンドウ満了時、以下の順序でチェックする（上からチェック、引っかかれば現状維持）。

```
① forcedCooldown 中か？
      YES → reason="forced_cooldown", 現状維持

② 前回切り替えから minSwitchSeconds(300秒) 未満か？
      YES → reason="min_interval_not_elapsed", 現状維持

③ bestEmaScore - secondEmaScore < deltaThreshold(0.15) か？
      YES → reason="delta_too_small", consecutiveWins をリセット, 現状維持

④ consecutiveWins[best] >= consecutiveThreshold(3) か？
      NO  → reason="waiting_for_consecutive(N/3)", 現状維持
      YES → 切り替え実行 → reason="threshold_reached"
                         → lastSwitchTs を更新
                         → consecutiveWins をリセット
                         → 通知発火 (NotificationHelper.showSwitchSuggestion)
```

**切り替えが実際に行われる条件のまとめ**:
- EMA スコア差が 0.15 以上
- その状態が 5回×3回 = 15観測 継続
- 前回切り替えから 300秒 経過
- 強制クールダウン中でない

---

## 8. 戻り値（DecisionResult）

```kotlin
data class DecisionResult(
    val chosenApId: String,      // "apA" or "apB"
    val rawScores: List<Float>,  // MLP の生ロジット（補正前）
    val emaScores: List<Float>,  // EMA 後のスコア
    val reason: String           // 切り替え判断の理由
)
```

`reason` の値一覧:

| 値 | 意味 |
|----|------|
| `pending_window(N/5)` | ウィンドウ未満（評価保留） |
| `forced_cooldown` | 強制クールダウン中 |
| `min_interval_not_elapsed` | 300秒インターバル未達 |
| `delta_too_small` | EMAスコア差が閾値未満 |
| `waiting_for_consecutive(N/3)` | 連続勝利カウント不足 |
| `threshold_reached` | **切り替え実行** |
| `none` | 初期状態 |

---

## 9. 設定パラメータ一覧（ap_config.json）

```json
{
  "apA": {"id": "apA", "ssid": "HFL_A24", ...},
  "apB": {"id": "apB", "ssid": "HFL_B",  ...},
  "emaAlpha":             0.6,
  "consecutiveThreshold": 3,
  "minSwitchSeconds":     300,
  "deltaThreshold":       0.15,
  "cycleWindow":          5
}
```

---

## 10. 現在の実装における既知の問題・制約

### ❶ 非接続 AP の品質情報が推論に使われていない（最重要）

`observe()` に渡される `tp` と `rtt` は**現在接続中の AP の実測値のみ**。
もう一方の AP（非接続側）の現在の品質（Ping, TP）はモデルに入力されない。

```
現状: input = [tp_現AP, rtt_現AP, app_onehot]
理想: input = [tp_AP-A, rtt_AP-A, tp_AP-B, rtt_AP-B, app_onehot]
```

つまりモデルは「AP-Bに切り替えたら良くなるか」を直接学習できておらず、
過去の学習データから統計的にそれを補完しているに過ぎない。

### ❷ 学習時の入力形式と推論時の入力形式の不一致

| | 学習時（`loadFlexible` + CSV） | 推論時（`DecisionEngine`） |
|-|-------------------------------|---------------------------|
| input[0..1] | z-score 正規化あり | 正規化なし（raw値） |
| input[2..5] | `one-hot(ap_label)` ← バグ | `one-hot(app_idx)` |

学習 CSV の `loadFlexible` が 4列 CSV で `app_idx` 列を捨て、
`ap_label` を one-hot に使うコーディングバグがある（`SimpleDataLoader.kt` L82-94）。

### ❸ needTP / needRTT の値が端末とサーバで異なる

- `ap_config.json`（端末）と `app.json`（サーバ・Ctrl+I推論）で値が異なる
- 満足度補正と推論スナップショットで異なる基準が使われている

### ❹ DecisionEngine が dead code

`experiment/DecisionEngine.kt` は完全実装済みだが、
**アプリのどこからも呼ばれていない**（`TrainingViewModel` 等から未接続）。
AP 切り替え推論は実質動作していない。

---

## 11. 改善案（AIエージェントへの指示用）

| 優先度 | 課題 | 修正場所 | 修正内容 |
|--------|------|----------|----------|
| **高** | 非接続 AP の品質を入力に加える | `DecisionEngine.kt`, `TrainingViewModel.kt` | 両 AP へのバックグラウンド Ping/TP 計測を追加し、`observe(tp_A, rtt_A, tp_B, rtt_B, appOneHot)` に拡張。inputSize を 6→8 に変更 |
| **高** | 学習 CSV の one-hot バグ修正 | `SimpleDataLoader.kt` L82-94 | `n == tpRttFeatures + 2` ブランチで `app_idx`（列[2]）を one-hot に、`ap_label`（列[3]）をラベルに正しく割り当てる |
| **中** | 推論時の正規化追加 | `ModelLoader.kt`, エッジサーバの配布処理 | 学習時の mean/std を `.norm.json` として保存・配布し `ModelLoader` に適用 |
| **中** | needTP/needRTT の一元化 | `app.json`, `ap_config.json` | 値を統一（または端末が起動時にサーバから取得）|
| **低** | outputSize を実 AP 数に合わせる | `LocalTrainer.kt`, edge server | `outputSize = 4` → `outputSize = 2` |

---

## 12. メインブランチ向け修正実装プロンプト

対象ブランチ: `main`（または実験系ではない安定ブランチ）
作成日: 2026-04-23

以下の 2 つの修正を **`HFL_terminals`** リポジトリに実装する。
優先度高の順に実施すること。

---

### 修正①: `SimpleDataLoader.kt` の one-hot バグ修正

#### 問題

`HFL_terminals/app/src/main/java/com/example/hfl_experiment/training/data/SimpleDataLoader.kt`
の `CsvLoader.loadFlexible()` 関数（`object CsvLoader` 内、L53〜）。

CSV の形式:
```
tp, rtt, app_idx, ap_label
0,  200, 2,       1        ← 通話(app_idx=2), AP-B選択(ap_label=1)
5,  0,   0,       0        ← ブラウザ(app_idx=0), AP-A選択(ap_label=0)
```

4 列 CSV のとき（`n == tpRttFeatures + 2` ブランチ, L82-94）:

```kotlin
// 現状（バグあり）
n == tpRttFeatures + 2 -> {
    val base = FloatArray(tpRttFeatures) { i -> pf(cols[i], i) }
    val a = cols[tpRttFeatures].toIntOrNull()      // app_idx
    val b = cols[tpRttFeatures + 1].toIntOrNull()  // ap_label
    if (b != null) {
        entries.add(RawEntry(base, b))  // label = ap_label ← ここまでは正しい
        // ★ 問題: app_idx (a) が捨てられる
    }
}
```

その後 L142-149 で one-hot を `re.label`（= `ap_label`）から構築するため、
**モデルへの入力 one-hot が「どのアプリか」ではなく「どの AP を選択したか」になる**。

これにより推論時（`DecisionEngine.observe()`）の `appOneHot` と学習時の one-hot が乖離する。

#### 修正方針

1. `RawEntry` data class に `appIdx: Int = -1` フィールドを追加する
2. `n == tpRttFeatures + 2` ブランチで `app_idx`（列[tpRttFeatures]）を `appIdx` として `RawEntry` に保存する
3. one-hot 構築ループ（L142-149 付近）で `re.appIdx >= 0` の場合は `re.appIdx` を、それ以外は従来通り `re.label` を one-hot インデックスとして使用する

#### 具体的な変更箇所

```
ファイル: SimpleDataLoader.kt
クラス:   object CsvLoader
関数:     loadFlexible()

変更1 - RawEntry に appIdx を追加（関数内 data class）:
  data class RawEntry(val base: FloatArray, var label: Int, var appIdx: Int = -1)

変更2 - n == tpRttFeatures + 2 ブランチ（L82-94）:
  n == tpRttFeatures + 2 -> {
      val base = FloatArray(tpRttFeatures) { i -> pf(cols[i], i) }
      val a = cols[tpRttFeatures].toIntOrNull()      // app_idx
      val b = cols[tpRttFeatures + 1].toIntOrNull()  // ap_label
      if (b != null) {
          entries.add(RawEntry(base, b, a ?: -1))    // label=ap_label, appIdx=app_idx
      } else if (a != null) {
          entries.add(RawEntry(base, a))
      } else {
          throw IllegalArgumentException("Unable to parse label/app idx for line: $line")
      }
  }

変更3 - one-hot 構築ループ（L142-149 付近）:
  val lbl = re.label.coerceAtLeast(0)
  val oneHotIdx = if (re.appIdx >= 0) re.appIdx else lbl   // ← app_idx 優先
  for (j in 0 until finalAppNum) feat[tpRttFeatures + j] = if (j == oneHotIdx) 1.0f else 0.0f
```

#### 検証方法

修正後、以下の CSV で `loadFlexible(file, 2, 4)` を呼び、
`DataSample.features[2..5]` が `app_idx` 由来の one-hot になっていることを確認する。

```
5.0,20,0,1   → features = [tp_norm, rtt_norm, 1.0, 0.0, 0.0, 0.0], label=1  (ブラウザ, AP-B)
1.0,80,2,0   → features = [tp_norm, rtt_norm, 0.0, 0.0, 1.0, 0.0], label=0  (通話,   AP-A)
```

---

### 修正②: `DecisionEngine` をアプリに接続する

#### 問題

`HFL_terminals/app/src/main/java/com/example/hfl_experiment/experiment/DecisionEngine.kt` は
完全実装済み（EMA・連続閾値・クールダウン）だが、**アプリのどこからも呼ばれていない（dead code）**。

AP 切り替え推論は実質動作していない状態である。

#### 接続の設計

呼び出し元は `TrainingViewModel` が適切。以下の情報がすでにそこに揃っている:

| 必要な情報 | TrainingViewModel での変数 |
|-----------|--------------------------|
| 実測 TP [Mbps] | `lastBandwidthMbps: Double?`（L224） |
| 実測 RTT [ms] | `lastMeasuredRttMs: Float?`（L223） |
| アプリ one-hot | `_assignedAppIndex: MutableStateFlow<Int?>`（L97） |
| モデル更新通知 | `modelUpdateChannel: Channel<String?>`（L449） |
| Config | `Config.load(context)` で取得可能 |

#### 修正手順

**ステップ 1: `TrainingViewModel` に `DecisionEngine` インスタンスを追加**

```kotlin
// TrainingViewModel のフィールド（クラス本体）
private var decisionEngine: DecisionEngine? = null
```

**ステップ 2: モデル更新時に `DecisionEngine` を初期化**

`notifyExternalModelUpdate()` が呼ばれた直後で `DecisionEngine` を（再）生成してモデルをロードさせる:

```kotlin
// notifyExternalModelUpdate() の末尾近くに追加
try {
    val cfg = com.example.hfl_experiment.experiment.Config.load(getApplication())
    if (decisionEngine == null) {
        decisionEngine = com.example.hfl_experiment.experiment.DecisionEngine(getApplication(), cfg)
    }
    // DecisionEngine の init{} が modelLoader.loadModel() を呼ぶため再生成で十分
    RealTimeLogger.i(TAG, "DecisionEngine initialized/refreshed for model update")
} catch (e: Exception) {
    RealTimeLogger.w(TAG, "DecisionEngine init failed: ${e.message}")
}
```

**ステップ 3: 計測後に `observe()` を呼ぶ**

`measureAndLogTerminalSatisfaction()` （L547 付近）の末尾で、計測値が揃った後に呼び出す:

```kotlin
// measureAndLogTerminalSatisfaction() 末尾に追加
try {
    val de = decisionEngine
    val tp = bandwidthMbps?.toFloat() ?: lastBandwidthMbps?.toFloat() ?: 0f
    val rtt = serverRttMsFloat ?: lastMeasuredRttMs ?: 0f
    val appIdx = _assignedAppIndex.value ?: 0
    val appNum = appConfigs?.size?.coerceAtLeast(4) ?: 4
    val oneHot = FloatArray(appNum) { i -> if (i == appIdx) 1f else 0f }
    if (de != null) {
        val result = de.observe(tp, rtt, oneHot)
        RealTimeLogger.i(TAG, "DecisionEngine.observe: chosenAP=${result.chosenApId} reason=${result.reason} ema=${result.emaScores}")
    }
} catch (e: Exception) {
    RealTimeLogger.w(TAG, "DecisionEngine.observe failed: ${e.message}")
}
```

**ステップ 4: ログ確認**

実装後に Logcat で以下のタグを確認:
- `DecisionEngine` — モデルロード・スコア・切り替え判断ログ
- `TrainingViewModel` — `observe` 呼び出しログ

expected ログ例:
```
DecisionEngine: initialized/refreshed for model update
DecisionEngine.observe: chosenAP=apA reason=pending_window(1/5) ema=[0.42, 0.38]
DecisionEngine.observe: chosenAP=apA reason=delta_too_small ema=[0.41, 0.39]
```

#### 注意事項

- `Config.load()` の戻り値型・メソッド名は `experiment/Config.kt` を参照して実際のシグネチャに合わせること
- `ap_config.json` の `modelFile` は `"model.tflite"` だが、エッジサーバから配布されるのは `meta.json` + `weight.bin` 形式。`ModelLoader.loadModel()` は assets ディレクトリを参照するため、**まず `ModelLoader` がエッジ配布済みのモデルを読めているか確認すること**
- `DecisionEngine.observe()` は同期呼び出し。`Dispatchers.IO` コルーチン内で呼ぶこと

---

### 修正後の動作確認チェックリスト

- [ ] `SimpleDataLoader` 修正: 4列 CSV の unit test で `features[2..5]` が `app_idx` one-hot になっている
- [ ] `DecisionEngine` 初期化: Logcat に `"DecisionEngine initialized"` ログが出る
- [ ] `observe()` 呼び出し: Logcat に `"DecisionEngine.observe"` ログが 5 回ごとに `delta_too_small` or `threshold_reached` が出る
- [ ] 30 ラウンド後: `reason=threshold_reached` が発生し AP 切り替え通知が出る
