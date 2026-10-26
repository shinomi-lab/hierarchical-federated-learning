# エッジサーバ推論 技術要綱レポート

> 対象プロジェクト: HFL_AP_selection / HFL_terminals / Serverside_HFL
> 作成日: 2026-04-27
> 目的: 実機研究における推論実装の妥当性判断資料

---

## 0. このレポートの読み方

本レポートは以下3つの視点を整理することで、推論実装の適切さを判断できる資料とする。

| 視点 | 内容 |
|---|---|
| **A. 現状把握** | シミュレーションと実機で「推論が今どこで行われているか」 |
| **B. 技術仕様** | エッジサーバが推論を担うとした場合の要件 |
| **C. 差異と問題** | シミュレーションと実機の不一致・既知のバグ |

---

## 1. システム全体構造

### 1-1. 階層構成

```
[ セントラルサーバ (Layer N) ]
        ↑ FedAvg集約 / トップダウン管理
[ 中間サーバ群 (Layer N-1) ]   ← mid_server: [3] = 3台
        ↑ FedAvg集約
[ エッジサーバ群 (Layer 0) ]   ← 端末の直近サーバ
        ↑ ローカル学習重みのアップロード
[ 端末（クライアント）群 ]     ← 70〜100台
```

`create_hierarchy.py` での実装:

```python
# Layer 0 = エッジサーバ（ユーザーと直接接続）
first_layer_server = self.system[0]
# 各エッジサーバは user_set（担当端末集合）と aggregated_weight（集約済み重み）を保持
layer_dict[server] = [user_set, self.nn_weights]
```

---

## 2. 推論の実行場所（現状）

### 2-1. シミュレーション（HFL_main.py）

**推論はセントラルの `global_model` が一括実行する。エッジサーバは推論に関与しない。**

```python
# HFL_main.py:454–477
global_model.eval()
testloader = DataLoader(test_dataset, batch_size=args.local_bs, shuffle=False)

for batch_idx, (images, labels) in enumerate(testloader):
    outputs = global_model(images)          # ← global_model（中央）が全端末分を一括推論
    _, pred_labels = torch.max(outputs, 1)  # argmax → AP_ID
    pred.append(pred_labels[index])

# 推論結果を各端末に適用
for item in range(len(TERMS)):
    TERMS[item].setSwitchAp(list_apNum[item])
```

エッジサーバ（`Structure.system[0]`）の役割はあくまで以下の3つのみ:

| 機能 | メソッド | 内容 |
|---|---|---|
| モデル配布 | `get_model(user_idx, download)` | エッジサーバの集約済み重みを端末に渡す |
| 重み集約 | `upload_weights(local_weights)` | 端末からの重みを FedAvg で集約 |
| モデル管理 | `model_management()` | 上位モデルが優秀なら下位に配布（バブルソート） |

### 2-2. 実機（HFL_terminals / Serverside_HFL）

**推論は Android 端末上の `DecisionEngine.kt` が実行する。**
エッジサーバはモデルの集約・配布のみ担い、推論判断自体は端末側で完結する。

```
[エッジ(Serverside_HFL)]
    FLラウンド完了 → global_model_mobile.pt 生成 → 端末へ配布
                                                        ↓
[端末(HFL_terminals)]
    ModelLoader がモデルをロード
        ↓ 計測イベント発生ごとに
    DecisionEngine.observe(tp, rtt, appOneHot)
        ↓
    MLPフォワードパス → 満足度補正 → EMA平滑化 → 切り替え判断
```

### 2-3. シミュレーション vs 実機 の推論場所比較

| 項目 | シミュレーション | 実機 |
|---|---|---|
| 推論実行場所 | セントラルサーバ（`global_model`） | 端末（`DecisionEngine.kt`） |
| 入力の収集 | シミュレーションデータ（仮想値） | 実測値（`NetworkBandwidthProbe`, Ping） |
| 推論タイミング | シミュレーションラウンドごと（バッチ） | 計測イベントごと（オンライン） |
| エッジサーバの役割 | 重み集約・配布のみ | 重み集約・配布のみ（推論に無関与） |

> **結論**: 現状の実装ではエッジサーバは「推論」を行っていない。推論はシミュレーションでは中央集権的に、実機では端末側でオンライン実行されている。

---

## 3. モデルアーキテクチャ仕様

### 3-1. シミュレーション側モデル（HFL_main.py:153–168）

```python
class Model(nn.Module):
    def __init__(self, input_size, hidden_size, output_size):
        self.layer1 = nn.Linear(input_size, hidden_size)   # Linear(6 → 70)
        self.layer2 = nn.Linear(hidden_size, hidden_size)  # Linear(70 → 70)
        self.layer3 = nn.Linear(hidden_size, output_size)  # Linear(70 → 3)
        self.relu   = nn.ReLU()

    def forward(self, x):
        x = self.relu(self.layer1(x))
        x = self.relu(self.layer2(x))
        x = nn.Softmax(dim=1)(self.layer3(x))
        return x
```

| パラメータ | 値 | 備考 |
|---|---|---|
| 入力次元 | 6 | [norm_tp, norm_rtt, app0, app1, app2, app3] |
| 隠れ層 | 70ユニット × 2層 | `confSim['termNum']` に依存 |
| 出力次元 | 3 | AP数（`confSim['apNumMax']`） |
| 活性化 | ReLU + Softmax | 出力は確率分布 |
| 損失関数 | CrossEntropyLoss | |
| 正規化層 | なし | |

### 3-2. 実機側モデル（Serverside_HFL: global_model_mobile.pt）

```
Linear(6 → 32) + LayerNorm(32) + ReLU
Linear(32 → 32) + LayerNorm(32) + ReLU
Linear(32 → 4)  ← Softmax（出力は確率）
```

| パラメータ | 値 | 備考 |
|---|---|---|
| 入力次元 | 6 | [tp, rtt, app0, app1, app2, app3] |
| 隠れ層 | 32ユニット × 2層 | シミュレーションより小型 |
| 出力次元 | 4 | ただし有効ラベルは {0, 1} の2クラスのみ |
| 正規化層 | LayerNorm あり | シミュレーションにはない |

### 3-3. モデル差異まとめ

| 項目 | シミュレーション | 実機 |
|---|---|---|
| 隠れ層サイズ | 70（端末数依存） | 32（固定） |
| LayerNorm | なし | あり |
| 出力クラス数 | 3（AP数） | 4（ラベルは2クラスのみ実質使用） |
| Softmax | forward()内 | forward()内 |

---

## 4. 推論入力パイプライン仕様

### 4-1. 入力ベクトルの構成（6次元）

```
input = [norm_tpNeed, norm_rttNeed, app_onehot[0], app_onehot[1], app_onehot[2], app_onehot[3]]
```

| 次元 | 内容 | シミュレーション | 実機 |
|---|---|---|---|
| [0] | TPニーズ | z-score正規化あり | **正規化なし（raw値）** |
| [1] | RTTニーズ | z-score正規化あり | **正規化なし（raw値）** |
| [2] | app one-hot[0] | appNum由来 | ap_label由来（バグ） |
| [3] | app one-hot[1] | appNum由来 | ap_label由来（バグ） |
| [4] | app one-hot[2] | appNum由来 | ap_label由来（バグ） |
| [5] | app one-hot[3] | appNum由来 | ap_label由来（バグ） |

### 4-2. シミュレーション側の正規化処理（HFL_main.py:439–447）

```python
# z-score 正規化（全端末の値を統計量で正規化）
np_tpNeed_norm  = (np_tpNeed  - np.mean(np_tpNeed))  / np.std(np_tpNeed)
np_rttNeed_norm = (np_rttNeed - np.mean(np_rttNeed)) / np.std(np_rttNeed)

# One-hot エンコード
APP_encoded = one_hot(t_appNum % total_APP)   # appNum → one-hot

# 結合
x_data = torch.cat([stack, APP_encoded], 1).float()  # shape: [N, 6]
```

> **重要**: シミュレーションでの正規化は「その時刻の全端末の統計量」でのz-score。
> 端末数や値の分布が変わると正規化後の値が変動する（バッチ正規化的な性質）。

### 4-3. アプリ種別と入力値の対応（app.json）

| appNum | appType | indicator | tpNeed | rttNeed |
|---|---|---|---|---|
| 0 | ブラウザ | tp | 5 Mbps | 0 ms |
| 1 | 動画 | tp | 10 Mbps | 0 ms |
| 2 | 通話 | rtt | 0 Mbps | 200 ms |
| 3 | 配信 | rtt | 0 Mbps | 100 ms |

TP系アプリは `rttNeed=0`、RTT系アプリは `tpNeed=0` として入力される。
6次元ベクトルのうち `[0]` か `[1]` のどちらかは常に0（正規化後は定数）になる。

---

## 5. 推論出力と基地局割り当て

### 5-1. シミュレーション側

```python
outputs = global_model(images)          # shape: [batch, 3]  ← 3AP分の確率
_, pred_labels = torch.max(outputs, 1)  # argmax → AP_ID ∈ {0, 1, 2}

for item in range(len(TERMS)):
    TERMS[item].setSwitchAp(list_apNum[item])  # 端末に割り当て適用
```

出力: 確率分布 `[P(AP0), P(AP1), P(AP2)]` → argmax → 整数AP ID

### 5-2. 実機側（DecisionEngine）

```
rawScores[AP-A, AP-B]
    ↓ 満足度補正（S = tp_measured/needTP または needRTT/rtt_measured）
correctedScores[AP-A, AP-B]
    ↓ EMA平滑化（alpha=0.6）
emaScores[AP-A, AP-B]
    ↓ 4段階ゲート判定
DecisionResult(chosenApId, rawScores, emaScores, reason)
```

実機では単純なargmaxではなく、**EMA平滑化 + 多段ゲート**で誤切り替えを防止している。

### 5-3. 切り替え判断ゲート（実機・4段階）

| ゲート | 条件 | 不成立時の動作 |
|---|---|---|
| ① 強制クールダウン | 実行中でないこと | 現状維持 |
| ② 最小インターバル | 前回切替から300秒以上 | 現状維持 |
| ③ スコア差閾値 | EMAスコア差 ≥ 0.15 | 現状維持 + consecutiveWinsリセット |
| ④ 連続勝利数 | 同一APが3ウィンドウ連続で優位 | 現状維持 |

**切り替え実行の最短条件**: スコア差0.15以上が5回×3回=15観測継続 かつ 300秒経過

### 5-4. reason フィールド一覧

| 値 | 意味 |
|---|---|
| `pending_window(N/5)` | ウィンドウ未満（評価保留） |
| `forced_cooldown` | 強制クールダウン中 |
| `min_interval_not_elapsed` | 300秒インターバル未達 |
| `delta_too_small` | EMAスコア差が閾値未満 |
| `waiting_for_consecutive(N/3)` | 連続勝利カウント不足 |
| `threshold_reached` | **切り替え実行** |

---

## 6. エッジサーバが推論を担う場合の技術要件

現状の実装では推論はセントラル（シミュレーション）または端末（実機）で行われているが、
**エッジサーバが推論を担う**設計にした場合、以下の要件が必要になる。

### 6-1. 入力収集プロトコル

```
端末 → エッジサーバ  (上り)
  送信データ: {termId, tpNeed, rttNeed, appNum, timestamp}
  方式: REST API / gRPC / WebSocket のいずれか
  タイミング: 計測イベント発生ごと（リアルタイム）
```

### 6-2. 推論実行要件

| 項目 | 要件 | 理由 |
|---|---|---|
| レイテンシ | < 100ms（推奨） | AP切り替え判断はリアルタイム性が必要 |
| 推論方式 | オンライン（1端末ずつ）or ミニバッチ | 端末数が多い場合はバッチ効率向上 |
| モデルキャッシュ | エッジサーバ上に最新モデルを常時保持 | 配布遅延なく推論可能にする |
| 正規化パラメータ | mean/std をモデルとともに保持 | 学習時z-scoreと推論時を一致させる |
| スレッドセーフ | 複数端末からの同時リクエスト対応 | 並列リクエストでモデル状態が競合しないよう |

### 6-3. 出力配信プロトコル

```
エッジサーバ → 端末  (下り)
  送信データ: {termId, assignedApId, scores, timestamp}
  方式: レスポンス（同期）or プッシュ通知（非同期）
```

### 6-4. モデル更新時の推論継続性

FLラウンドによるモデル更新中も推論リクエストが来た場合:

| 方式 | 説明 |
|---|---|
| 推奨: Blue/Green | 旧モデルと新モデルを並行保持し、更新完了後に切り替え |
| 最低限 | モデル更新中は旧モデルで応答し続ける |
| 禁止 | モデル更新中に推論を停止する（可用性が著しく低下する） |

---

## 7. 既知の問題と実装の妥当性評価

### 7-1. シミュレーション側の問題

| # | 問題 | 箇所 | 深刻度 |
|---|---|---|---|
| S-1 | 推論時の正規化がバッチ依存（端末数・値域が変わると不安定） | `HFL_main.py:439–442` | 中 |
| S-2 | `calLink()` の初期RTTがコード内ハードコード（`ap.json` 未連携） | `cal.py:86` | 中 |
| S-3 | `sim.json` の `initRTT` が推論フローで未使用（死に変数） | `cal.py:25–26` | 低 |
| S-4 | `global_model` がバッチ全端末分を一括推論（実機のオンライン推論と乖離） | `HFL_main.py:454–477` | 中 |

### 7-2. 実機側の問題

| # | 問題 | 箇所 | 深刻度 |
|---|---|---|---|
| R-1 | **学習時: z-score正規化あり / 推論時: 正規化なし** の不一致 | `SimpleDataLoader.kt` / `DecisionEngine.kt` | **高** |
| R-2 | **学習CSVの one-hot が `ap_label` 由来（`app_idx` が破棄されるバグ）** | `SimpleDataLoader.kt:82–94` | **高** |
| R-3 | `DecisionEngine.kt` が完成しているが**アプリから未接続（dead code）** | `TrainingViewModel.kt` | **高** |
| R-4 | 出力次元が4だが実際のラベル域は {0, 1} の2クラスのみ | `LocalTrainer.kt` / Serverside | 中 |
| R-5 | 非接続AP（もう一方のAP）の品質が推論入力に含まれない | `DecisionEngine.kt:observe()` | 中 |
| R-6 | `ap_config.json`（端末）と `app.json`（サーバ）で needTP/needRTT の値が異なる | 両設定ファイル | 中 |

### 7-3. シミュレーション vs 実機の入出力不一致

```
【学習データ（CSV: tpNeed, rttNeed, appNum, AP_ID）】
  └─ 生成元: hungarian_main.py（ハンガリアン法の最適解）
  └─ tpNeed / rttNeed は app.json の needTP / needRTT（固定値）
  └─ 正規化: z-score（全端末バッチで計算）

【シミュレーション推論入力】
  └─ 同じ固定値（needTP/needRTT）をz-score正規化して入力
  ✅ 学習と整合

【実機推論入力（DecisionEngine）】
  └─ 実測値（tp_measured [Mbps], rtt_measured [ms]）をそのまま入力
  ❌ 正規化なし → 学習と不一致
  ❌ one-hot が app_idx ではなく ap_label 由来 → 意味が異なる
```

---

## 8. 推論実装の妥当性判断チェックリスト

### カテゴリA: 入力の整合性

- [ ] **A-1** 学習時と推論時で入力の正規化方式が同一か（z-score の mean/std が同じか）
- [ ] **A-2** one-hotエンコードのインデックスが学習時と推論時で同一か（`app_idx` 由来か）
- [ ] **A-3** tpNeed / rttNeed の値がサーバ・端末間で統一されているか
- [ ] **A-4** 入力次元がモデルの `input_size` と一致しているか（現状: 6次元）

### カテゴリB: モデルの整合性

- [ ] **B-1** 出力次元（`output_size`）が実際のAP数と一致しているか
- [ ] **B-2** 学習時と推論時でモデルアーキテクチャ（層構成）が同一か
- [ ] **B-3** LayerNorm の有無が学習・推論間で一致しているか

### カテゴリC: 推論パイプラインの動作確認

- [ ] **C-1** `DecisionEngine.observe()` が実際に呼ばれているか（dead codeでないか）
- [ ] **C-2** EMAスコアが複数観測をまたいで適切に蓄積されているか
- [ ] **C-3** 切り替え判断ゲート（4段階）が意図通りに機能しているか
- [ ] **C-4** モデル更新後に推論が最新モデルを参照しているか

### カテゴリD: 推論精度の評価

- [ ] **D-1** テストデータでの正確度（Accuracy）が許容水準か
- [ ] **D-2** Precision / Recall / F1 Score が AP ごとに偏っていないか
- [ ] **D-3** 実際の割り当て結果で調和平均満足度がランダム割当より改善しているか

---

## 9. 推論フロー全体図（シミュレーション）

```
[教師データ生成: hungarian_main.py]
  ランダム初期化 → calLink（TP/RTT計算）→ ハンガリアン法 → CSV保存
  CSV列: tpNeed, rttNeed, appNum, AP_ID（0-indexed）
          ↓
[学習フェーズ: HFL_main.py]
  CSV読み込み → z-score正規化 → one-hot → TensorDataset
          ↓ 80/20 train/test分割
  HFL学習ループ（global_model が FedAvg で更新）
          ↓
[推論フェーズ: HFL_main.py]
  for シミュレーション時刻 t:
    ランダム初期化（randAp, randApp）
    各端末の needTP / needRTT / appNum 取得
    z-score正規化 + one-hot エンコード → 6次元ベクトル
          ↓
    global_model.eval()
    outputs = global_model(x)       → shape: [N, 3]（N=端末数）
    AP_ID = argmax(outputs, dim=1)  → shape: [N]
          ↓
    各端末に setSwitchAp(AP_ID)
    calLink → calSatis（調和平均満足度を評価）
```

---

## 10. 実機研究向け推奨改善事項

### 【最優先】R-1 + R-2: 学習・推論の入力整合性確保

```
1. SimpleDataLoader.kt の one-hot バグ修正
   → 4列CSV で app_idx を one-hot に、ap_label をラベルに正しく割り当てる
   → RawEntry に appIdx フィールドを追加し one-hot 構築ループで優先使用

2. 正規化パラメータの配布
   → 学習時の mean/std を weight.bin.norm.json として保存・端末へ配布
   → ModelLoader で正規化を適用してからモデルに入力する
```

### 【最優先】R-3: DecisionEngine の接続

```
TrainingViewModel.notifyExternalModelUpdate() 後に DecisionEngine を初期化
measureAndLogTerminalSatisfaction() 内で observe() を呼び出す
Dispatchers.IO コルーチン内で同期呼び出し
```

### 【中優先】B-1: 出力次元の統一

```
AP数が2局なら output_size = 2 に統一
（現状: output_size=4 だがラベルは {0,1} のみ → 無駄な次元が2つある）
```

### 【中優先】A-3: needTP/needRTT の一元管理

```
app.json（サーバ）と ap_config.json（端末）の値を統一
または端末起動時にサーバから最新値を取得する仕組みを設ける
```

---

*本レポートは HFL_AP_selection-main（シミュレーション）/ HFL_terminals / Serverside_HFL の各コードを直接調査して作成した。*
*既存ドキュメント `docs/inference/ap_switching_inference.md` および `docs/inference/implementation_summary.md` の内容を統合・整理した。*
