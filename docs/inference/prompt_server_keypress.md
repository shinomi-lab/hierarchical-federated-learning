# タスク: サーバターミナルのキー操作による AP 切り替え推論の実装

## 背景・現状

HFL（階層型連合学習）システムでは Android 端末がローカル学習を行い、学習済みモデルが
中央サーバ (`Serverside_HFL`) に集約される。
集約後のグローバルモデルは `state/global_model_mobile.pt` に保存される。

### 現在できていること
- FL学習の実行・集約・モデル配布は動作している
- `start.py` に `Ctrl+I`（Tab = `\x09`）キーで `_do_inference_snapshot()` が呼べる実装が存在するが、
  **リンターによって定期的に消されており不安定**

### 現在できていないこと
- Android の `DecisionEngine`（`experiment/DecisionEngine.kt`）は実装済みだが、
  **アプリのコードのどこからも呼ばれていない**（未接続の dead code）
- サーバ側の推論スナップショット（`_do_inference_snapshot`）は
  `needTP`/`needRTT` の固定値しか使わず、実際のネットワーク計測値が反映されない
- 端末ごとの AP 推薦結果が表示されない

---

## 実現したいこと

**サーバターミナルで特定のキーを押すと、その時点でのグローバルモデルによる
AP 切り替え推薦結果が端末ごとに表示される。**

### 想定する出力イメージ

```
─── AP 切り替え推論スナップショット（ラウンド 12） ───

  端末          現AP   アプリ   TP実測   RTT実測    AP-A    AP-B   推薦   根拠
  ───────────────────────────────────────────────────────────────────────────
  terminal-01  AP-A   通話      8.3Mbps   22ms     43.2%   56.8%  AP-B  score_diff=0.136
  terminal-02  AP-A   動画     12.1Mbps   18ms     61.4%   38.6%  AP-A  score_diff=0.228
  terminal-03  AP-B   ブラウザ  5.6Mbps   35ms     49.1%   50.9%  AP-B  score_diff=0.018 (差小)
  terminal-04  AP-B   配信      3.2Mbps   41ms     55.3%   44.7%  AP-A  score_diff=0.106

  注: score_diff < 0.15 (deltaThreshold) は切り替え閾値未満
```

---

## 実装仕様

### 1. キーバインド

`Serverside_HFL/start.py` の `_start_keyboard_listener()` に追加する。

- **キー**: `Ctrl+I`（`\x09`、Tab と同値）
- **既存パターンに合わせて実装**:
  ```python
  elif ch == b'\x09':
      _termios.tcsetattr(fd, _termios.TCSADRAIN, old_settings)
      _do_inference_snapshot()
      _tty.setraw(fd)
  ```
- 既存キー: `Ctrl+G`=グレースフルリセット、`Ctrl+L`=トポロジ表示

### 2. `_do_inference_snapshot()` 関数の仕様

`_start_keyboard_listener` の直前（約 line 774 付近）に定義する。

#### 2-1. モデルロード

```python
model_path = ROOT / "state" / "global_model_mobile.pt"
# torch.load で state_dict を取得
# キー: layer1.weight[32,6], layer1.bias[32], norm1.weight[32], norm1.bias[32],
#       layer2.weight[32,32], layer2.bias[32], norm2.weight[32], norm2.bias[32],
#       out.weight[4,32], out.bias[4]
```

モデルが存在しない場合はエラーメッセージを表示して終了。

#### 2-2. 端末ごとの入力データ取得（優先順位あり）

各端末のネットワーク計測値を以下の優先順位で取得する:

**優先①: research log JSONL から最新レコードを取得**
```
Serverside_HFL/received_files/research_log/research_YYYYMMDD.jsonl
```
各行の JSON から以下を取得:
- `terminal_id`
- `tp_measured_mbps`（実測スループット）
- `rtt_measured_ms`（実測 RTT）
- `app_type`（アプリ種別文字列: "ブラウザ", "動画", "通話", "配信"）
- `virtual_router_id`（現在接続中の AP ID: "HFL_A24"→AP-A, "HFL_B"→AP-B）

同一 `terminal_id` の最新レコードを使用。

**優先②: エッジサーバのトポロジ API から取得**
```
GET http://127.0.0.1:{edge_port}/admin/topology_snapshot
```
RTT/TP が取得できない場合のフォールバック。

**優先③: app.json の needTP/needRTT を使用**
どちらも取得できない場合は固定値で推論（現状の動作）。

#### 2-3. フォワードパス

```python
# 入力: 6次元
input = [tp_measured, rtt_measured, app_onehot[0], app_onehot[1], app_onehot[2], app_onehot[3]]

# app_onehot: app.json の appType 文字列 → index を変換
# "ブラウザ"=0, "動画"=1, "通話"=2, "配信"=3

# フォワードパス (numpy, float64)
# Linear(6→32) → LayerNorm → ReLU → Linear(32→32) → LayerNorm → ReLU → Linear(32→4) → Softmax
probs = softmax(forward(input))  # [P(AP-A), P(AP-B), P(AP-C), P(AP-D)]

# 有効クラスは index 0 (AP-A) と 1 (AP-B) のみ
best_ap_idx = argmax(probs[:2])
score_diff  = abs(probs[0] - probs[1])
```

#### 2-4. 満足度補正（オプション）

`ap_config.json` の `needTP`/`needRTT` と実測値を使い、スコアに補正をかける:
```
corrected_score[i] = raw_score[i] × S
S = tp_measured / needTP   （TPアプリ）
S = needRTT / rtt_measured （RTTアプリ）
S = clip(S, 0, 1)
```

#### 2-5. 表示フォーマット

- ヘッダ + 端末ごとの 1 行表示
- `score_diff < deltaThreshold(0.15)` の場合は「差小」と注記
- 全角文字は `unicodedata.east_asian_width` で display-width 基準パディング
- `_green()` / `_yellow()` / `_red()` / `_cyan()` / `_bold()` の色ヘルパーを使用（start.py 内に既存）
- フッタにラウンド番号・モデルパス・データソースを表示

---

## 関連ファイル

| ファイル | 役割 |
|----------|------|
| `Serverside_HFL/start.py` | **実装対象**。`_do_inference_snapshot()` と Ctrl+I ハンドラを追加 |
| `Serverside_HFL/state/global_model_mobile.pt` | 推論に使うグローバルモデル（state dict） |
| `Serverside_HFL/app.json` | アプリ定義（appType, needTP, needRTT） |
| `Serverside_HFL/received_files/research_log/research_*.jsonl` | 端末ごとの最新計測値ソース |
| `Serverside_HFL/edge_server/state.py` | `current_edge_state.round` でラウンド番号を取得可能 |

---

## モデルアーキテクチャ（参考）

```
state dict キー:
  layer1.weight [32, 6]   layer1.bias [32]
  norm1.weight  [32]      norm1.bias  [32]
  layer2.weight [32, 32]  layer2.bias [32]
  norm2.weight  [32]      norm2.bias  [32]
  out.weight    [4, 32]   out.bias    [4]

フォワードパス:
  h1 = ReLU(LayerNorm(W1 @ x + b1))
  h2 = ReLU(LayerNorm(W2 @ h1 + b2))
  logits = Wo @ h2 + bo          # shape: [4]
  probs  = softmax(logits)       # 有効: probs[0]=AP-A, probs[1]=AP-B
```

---

## 既知の制約（実装時に注意）

1. **入力の正規化なし**: 学習時は z-score 正規化されているが、推論時は raw 値を入力する
   （DecisionEngine の既存実装に合わせる）
2. **one-hot の意味論的ズレ**: 学習 CSV では ap_label で one-hot を構築しているが、
   推論では app_idx で one-hot を構築する（DecisionEngine と同じ方式）
3. **非接続 AP の計測値なし**: 推論入力は現在接続中 AP の計測値のみ
4. **リンターによる巻き戻しに注意**: 過去に同関数を実装したところリンターが削除する事象が発生。
   関数が消えていたら再実装が必要

---

## 実装チェックリスト

- [ ] `_do_inference_snapshot()` 関数を `_start_keyboard_listener` の直前に定義
- [ ] research log JSONL から端末ごとの最新 tp/rtt/app_type を取得するロジック
- [ ] グローバルモデルのロードとフォワードパス（numpy, float64, errstate 抑制）
- [ ] 端末ごとの推薦 AP と score_diff の表示
- [ ] `_start_keyboard_listener` の `elif ch == b'\x09':` ハンドラ追加
- [ ] ドキュメント冒頭の起動後キー説明に `Ctrl+I` を追記
