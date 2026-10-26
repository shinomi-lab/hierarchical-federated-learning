# 推論システム実装まとめ（branch: fully-controled-inference）

作成日: 2026-04-23
対象リポジトリ: `HFL_terminals` / `Serverside_HFL`（両リポジトリ同一ブランチ）

---

## 1. 全体構成

HFL システムの推論（AP 選択）は以下の 3 層で行われる。

```
Android 端末 (DecisionEngine)
  └─ ローカルモデルで AP スコアを計算 → AP 切り替え判断
        ↑ モデルは中央サーバから配布

中央サーバ
  └─ グローバルモデル state/global_model_mobile.pt を管理
  └─ Ctrl+I ショートカットで推論スナップショットを即時確認可能（NEW）
```

---

## 2. モデルアーキテクチャ

### 2-1. 実際のモデル構造（state dict から確認済み）

```
Linear(6 → 32) + LayerNorm(32) + ReLU
Linear(32 → 32) + LayerNorm(32) + ReLU
Linear(32 → 4)   ← 出力層（Softmax で確率化）
```

- **入力次元**: 6
- **隠れ層**: 32 ユニット × 2 層
- **出力次元**: 4（クラス 0〜3）
- **有効クラス**: 0（AP-A）と 1（AP-B）の 2 クラスのみ（ラベルが {0,1} の 2 値）

### 2-2. state dict キー名（Serverside が使う標準キー）

| キー | 形状 | 内容 |
|------|------|------|
| `layer1.weight` | [32, 6] | 第1層重み |
| `layer1.bias`   | [32]    | 第1層バイアス |
| `norm1.weight`  | [32]    | LayerNorm γ |
| `norm1.bias`    | [32]    | LayerNorm β |
| `layer2.weight` | [32, 32] | 第2層重み |
| `layer2.bias`   | [32]    | 第2層バイアス |
| `norm2.weight`  | [32]    | LayerNorm γ |
| `norm2.bias`    | [32]    | LayerNorm β |
| `out.weight`    | [4, 32] | 出力層重み |
| `out.bias`      | [4]     | 出力層バイアス |

---

## 3. 推論入力フォーマット

### 3-1. ランタイム（Android DecisionEngine）

```kotlin
// DecisionEngine.kt: observe(tp, rtt, appOneHot)
val input = FloatArray(2 + appOneHot.size)
input[0] = tp           // 実測スループット [Mbps]
input[1] = rtt          // 実測 RTT [ms]
input[2..5] = appOneHot // 現在のアプリ one-hot (4次元、正規化なし)
```

- **正規化**: なし（raw値をそのまま入力）
- **appOneHot**: アプリインデックス 0〜3 の one-hot

### 3-2. app.json の 4 アプリとインデックス対応

| idx | appType | indicator | needTP | needRTT |
|-----|---------|-----------|--------|---------|
| 0 | ブラウザ | tp  |  5 Mbps |    0 ms |
| 1 | 動画     | tp  | 10 Mbps |    0 ms |
| 2 | 通話     | rtt |  0 Mbps |  200 ms |
| 3 | 配信     | rtt |  0 Mbps |  100 ms |

---

## 4. 学習データの入力フォーマット（重要な既知の不一致）

### 4-1. 学習 CSV の実形式

```
tp, rtt, app_idx, ap_label
0,  200, 2,       1        ← 通話アプリ, AP-B選択
0,  100, 3,       2        ← 配信アプリ, AP-B選択
5,  0,   0,       0        ← ブラウザ, AP-A選択
```

- `ap_label` の値域: {1, 2}（1-indexed） → loadFlexible でシフト後 {0, 1}

### 4-2. loadFlexible() の動作（4列 CSV のとき）

```kotlin
// SimpleDataLoader.kt: n == tpRttFeatures + 2 ブランチ
base  = [tp, rtt]              // 列[0], 列[1]
// 列[2] = app_idx は破棄される（バグ）
label = cols[3].toInt()        // 列[3] = ap_label

// one-hot は ap_label から構築される
feat[2 + j] = if (j == label) 1f else 0f  // ap_label の one-hot (4次元)
```

### 4-3. 不一致のまとめ

| | 学習時 | 推論時 (DecisionEngine) |
|-|--------|-------------------------|
| input[2..5] | `one-hot(ap_label)` | `one-hot(app_idx)` |
| input[0..1] | z-score 正規化あり | 正規化なし (raw) |

**結論**: モデルは「ap_label の one-hot → ap_label を予測」という自己参照的なマッピングを学習している。推論時に app_idx one-hot を入力しても、インデックス 0 と 1 は ap_label 0・1 と一致するため、ブラウザ・動画については一定の意味をもつが、通話・配信（インデックス 2・3）は学習時に 0 だったため意味のない出力になる。

---

## 5. サーバ側 Ctrl+I 推論ショートカット（NEW）

### 5-1. 実装場所

- **ファイル**: `Serverside_HFL/start.py`
- **関数**: `_do_inference_snapshot()` （`_start_keyboard_listener` 直前に定義）
- **キーバインド**: `Ctrl+I`（rawターミナルでは Tab = `\x09` と同値）

### 5-2. 動作フロー

```
Ctrl+I 押下
  → termios を normal に戻す
  → _do_inference_snapshot() 呼び出し
      1. received_files/research_log/research_*.jsonl から端末ごとの最新レコード取得
         (tp_measured_mbps, rtt_measured_ms, app_type, virtual_router_id)
      2. グローバルモデル state/global_model_mobile.pt をロード (torch.load)
      3. 端末ごとに入力ベクトルを構築
         inp = [tp_measured, rtt_measured, app_onehot[0..3]]
      4. NumPy でフォワードパス実行
         Linear → LayerNorm → ReLU → Linear → LayerNorm → ReLU → Linear → Softmax
      5. 端末ごとの推薦 AP と score_diff を表示
         research log がない場合は app.json の needTP/needRTT で全アプリ種別を表示
  → termios を raw に戻す
```

### 5-3. 出力例（research log あり）

```
─── AP 切り替え推論スナップショット（ラウンド 67） ───

  端末           現AP    アプリ    TP実測     RTT実測     AP-A     AP-B  推薦  根拠
  ────────────────────────────────────────────────────────────────────────────────────
  terminal-01   AP-A   配信      1.3Mbps     74ms    36.8%    28.7%  AP-A  score_diff=0.081 (差小)
  terminal-02   AP-A   ブラウザ   3.4Mbps     35ms    36.8%    28.8%  AP-A  score_diff=0.080 (差小)
  terminal-03   AP-A   ブラウザ   3.5Mbps     33ms    36.8%    28.8%  AP-A  score_diff=0.080 (差小)
  terminal-04   AP-A   ブラウザ   2.0Mbps     31ms    36.7%    28.8%  AP-A  score_diff=0.078 (差小)

  注 score_diff < 0.15 (deltaThreshold) は切り替え閾値未満

  モデル: global_model_mobile.pt  arch: Linear(6→32)→LN→ReLU×2→Linear(32→4)
  データソース: research_log (2 files)
```

- **AP-A / AP-B** の確率が実験の意味のある出力（AP-C / AP-D はラベルなし）
- score_diff が全端末 < 0.15 なのはモデルがまだ均質な出力をしているため（学習収束前）
- 30 ラウンド学習後に確率分布が変化する → 学習収束の指標として使える

### 5-4. 実装上の注意点

- `torch.load(..., weights_only=False)` を使用（TorchScript 形式の場合も考慮済み）
- float32 → float64 にキャストして精度確保
- `np.errstate(divide="ignore", over="ignore", invalid="ignore")` で数値警告を抑制
- 全角文字対応: `unicodedata.east_asian_width` で display-width 基準パディング
- モデルファイル欠損・キー不一致時は例外を捕捉してエラーメッセージ表示で安全終了
- **リンターによる巻き戻しに注意**: 過去に複数回削除されている。消えていたら再実装が必要

---

## 6. 推論パイプラインの課題（将来対応すべき点）

| 優先度 | 問題 | 場所 | 対策案 |
|--------|------|------|--------|
| 高 | `loadFlexible` が 4列 CSV で `app_idx` を破棄し `ap_label` で one-hot を構築するバグ | `SimpleDataLoader.kt` L82〜94 | `n == tpRttFeatures + 2` ブランチで `app_idx` を one-hot に、`ap_label` をラベルに正しく割り当てる |
| 高 | DecisionEngine が dead code（アプリ内で一度も呼ばれていない） | `TrainingViewModel.kt` | `notifyExternalModelUpdate()` 後に初期化、`measureAndLogTerminalSatisfaction()` 内で `observe()` 呼び出し |
| 高 | 学習時は z-score 正規化、推論時は正規化なし | `loadFlexible` / `DecisionEngine` | `.norm.json` ファイルで mean/std を保存・配布し `ModelLoader` に読み込ませる |
| 中 | outputSize=4 だがラベルは {0,1} の 2 クラス | Android `LocalTrainer` / edge server | outputSize を実 AP 数（2）に合わせる |
| 低 | 推論スナップショットが通常モデル（bootstrap）では差異が小さい | `start.py` | 30ラウンド後の結果と並べて差分表示する機能追加 |
