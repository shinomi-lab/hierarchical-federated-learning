# HFL_AP_selection プロジェクト 推論フロー参考資料

> 実機研究向け参考プロンプト
> 対象プロジェクト: `HFL_AP_selection-main`
> 作成日: 2026-04-22

---

## 概要

本プロジェクトは **階層型連合学習（Hierarchical Federated Learning: HFL）** を用いて、**無線端末が最適な基地局（Access Point: AP）へ接続する選択問題**を解くシステムです。

PyTorchで構築したニューラルネットワークが、端末の通信需要（スループット・遅延）とアプリケーション種別から最適なAP IDを予測します。

---

## システム構成ファイルマップ

```
HFL_AP_selection-main/
├── HFL_main.py            # メインエントリポイント（学習・推論・シミュレーション）
├── models.py              # モデルアーキテクチャ定義
├── update.py              # ローカル更新・推論関数（LocalUpdate クラス）
├── create_hierarchy.py    # 階層構造（エッジ/中間/セントラルサーバー）構築
├── utils.py               # ユーティリティ（average_weights 等）
├── sampling.py            # データサンプリング（IID / Non-IID）
├── cal.py                 # 通信品質・満足度計算
├── hungarian_kai.py       # ハンガリアンアルゴリズム（比較用）
├── term.py                # 端末クラス
├── ap.py                  # 基地局クラス
├── create.py              # AP・端末インスタンス生成
├── rand.py                # ランダム割り当て
├── options.py             # コマンドライン引数
├── graph.py               # グラフ出力
├── output.py              # テキスト出力
├── sim.json               # シミュレーション設定
├── app.json               # アプリケーション仕様
├── ap.json                # 基地局仕様
├── 70_event.csv           # 70端末イベントデータ
└── 100_event.csv          # 100端末イベントデータ
```

---

## 設定ファイル詳細

### `sim.json` — シミュレーション全体設定

```json
{
  "termNum": 70,          // 端末数
  "appNumMax": 4,         // アプリケーション種類数
  "apNumMax": 3,          // 基地局数（= モデル出力クラス数）
  "termCapa": 100,        // 基地局の収容能力
  "type": "A",            // シミュレーションタイプ
  "appUseSec": 90,        // アプリケーション利用時間 [秒]
  "simNumTime": 10,       // シミュレーション繰り返し回数
  "initRTT": 20,          // 初期RTT [ms]
  "algo": {
    "upperLimit": true,
    "algoNum": 7          // 7 = HFLモデル推論による選択
  }
}
```

### `ap.json` — 基地局仕様（3局）

```json
[
  {"id": 0, "dataLimit": 7168, "price": 7000},  // AP0: 高容量 7168 Mbps
  {"id": 1, "dataLimit": 5120, "price": 5000},  // AP1: 中容量 5120 Mbps
  {"id": 2, "dataLimit": 3072, "price": 3000}   // AP2: 低容量 3072 Mbps
]
```

### `app.json` — アプリケーション仕様（4種類）

```json
[
  {"appType": "ブラウザ", "indicator": "tp",  "needTP": 5,   "needRTT": 0},
  {"appType": "動画",     "indicator": "tp",  "needTP": 10,  "needRTT": 0},
  {"appType": "通話",     "indicator": "rtt", "needTP": 0,   "needRTT": 200},
  {"appType": "配信",     "indicator": "rtt", "needTP": 0,   "needRTT": 100}
]
```

---

## モデルアーキテクチャ

**ファイル:** `models.py` / `HFL_main.py:152–192`

```python
class Model(nn.Module):
    def __init__(self, input_size, hidden_size, output_size):
        super().__init__()
        self.layer1 = nn.Linear(input_size, hidden_size)
        self.layer2 = nn.Linear(hidden_size, hidden_size)
        self.layer3 = nn.Linear(hidden_size, output_size)
        self.relu   = nn.ReLU()

    def forward(self, x):
        x = self.relu(self.layer1(x))
        x = self.relu(self.layer2(x))
        x = self.layer3(x)
        return nn.Softmax(dim=1)(x)
```

| パラメータ | 値 | 説明 |
|---|---|---|
| `input_size` | `train_x_data.shape[1]` | 通常 6〜7 次元 |
| `hidden_size` | `confSim['termNum']` | 70 (端末数に依存) |
| `output_size` | `confSim['apNumMax']` | 3 (基地局数) |
| 損失関数 | `CrossEntropyLoss` | 多クラス分類 |
| オプティマイザ | `Adam` (lr=0.001) | デフォルト |
| 出力 | `Softmax` → `argmax` | 基地局 ID ∈ {0, 1, 2} |

---

## 入力データ構造

### CSVフォーマット（`70_event.csv` / `100_event.csv`）

| 列名 | 型 | 説明 |
|---|---|---|
| `tpNeed` | float | 必要スループット [Mbps] |
| `rttNeed` | float | 必要往復遅延時間 [ms] |
| `appNum` | int | アプリケーション番号（0〜3） |
| `combiApTermArray` | int | 正解ラベル（接続すべき AP ID） |

### 前処理パイプライン（`HFL_main.py:84–141`）

```python
# 1. CSVロード
df = pd.read_csv('70_event.csv')

# 2. 列抽出
np_tpNeed  = df['tpNeed'].values
np_rttNeed = df['rttNeed'].values
np_appNum  = df['appNum'].values
np_labels  = df['combiApTermArray'].values

# 3. z-score 正規化
np_tpNeed_norm  = (np_tpNeed  - np.mean(np_tpNeed))  / np.std(np_tpNeed)
np_rttNeed_norm = (np_rttNeed - np.mean(np_rttNeed)) / np.std(np_rttNeed)

# 4. Tensor 変換
t_tpNeed_norm  = torch.from_numpy(np_tpNeed_norm).float()
t_rttNeed_norm = torch.from_numpy(np_rttNeed_norm).float()
t_appNum       = torch.tensor(np_appNum, dtype=torch.long)

# 5. Stack + One-hot エンコード
stack       = torch.stack([t_tpNeed_norm, t_rttNeed_norm], axis=1)
APP_encoded = one_hot(t_appNum % total_APP, num_classes=total_APP).float()
x_data      = torch.cat([stack, APP_encoded], dim=1)
# 例: [norm_tp, norm_rtt, app0, app1, app2, app3] → 6次元

# 6. train/test 分割（80/20）と TensorDataset 化
train_dataset = TensorDataset(x_train, y_train)
test_dataset  = TensorDataset(x_test,  y_test)
```

---

## 推論フロー（モデルによる最適 AP 選択）

**ファイル:** `HFL_main.py:426–492`

### ステップ 1: シミュレーション時刻ごとの入力準備

```python
# 各端末の現在の通信需要を取得
for item in range(len(TERMS)):
    np_tpNeed[item]  = TERMS[item].app.tpNeed   # 必要スループット
    np_rttNeed[item] = TERMS[item].app.rttNeed  # 必要遅延
    np_appNum[item]  = TERMS[item].appNum        # アプリ番号
```

### ステップ 2: リアルタイム正規化

```python
np_tpNeed_norm  = (np_tpNeed  - np.mean(np_tpNeed))  / np.std(np_tpNeed)
np_rttNeed_norm = (np_rttNeed - np.mean(np_rttNeed)) / np.std(np_rttNeed)
```

### ステップ 3: モデル推論

```python
global_model.eval()
testloader = DataLoader(test_dataset, batch_size=args.local_bs, shuffle=False)

pred = []
with torch.no_grad():
    for batch_idx, (images, labels) in enumerate(testloader):
        images = images.to(device)
        outputs = global_model(images)          # shape: [batch, 3]
        _, pred_labels = torch.max(outputs, 1)  # argmax → AP ID
        pred.append(pred_labels)
```

### ステップ 4: AP 割り当て適用

```python
for item in range(len(TERMS)):
    TERMS[item].setSwitchAp(list_apNum[item])  # 端末に最適AP IDをセット
```

### 推論の数学的フロー

```
入力: x = [norm_tpNeed, norm_rttNeed, one_hot(appNum)]  ← 6次元ベクトル
        ↓
   Linear(6 → 70) + ReLU
        ↓
   Linear(70 → 70) + ReLU
        ↓
   Linear(70 → 3)
        ↓
   Softmax(dim=1)  → [p_AP0, p_AP1, p_AP2]  ← 確率分布
        ↓
   argmax          → AP_id ∈ {0, 1, 2}       ← 最適基地局 ID
```

---

## 推論精度評価関数

**ファイル:** `update.py:88–110`

```python
class LocalUpdate:
    def inference(self, model):
        model.eval()
        loss, total, correct = 0.0, 0.0, 0.0
        criterion = nn.CrossEntropyLoss()

        for batch_idx, (images, labels) in enumerate(self.testloader):
            images, labels = images.to(self.device), labels.to(self.device)
            outputs    = model(images)
            batch_loss = criterion(outputs, labels)
            loss      += batch_loss.item()

            _, pred_labels = torch.max(outputs, 1)
            correct += torch.sum(torch.eq(pred_labels, labels)).item()
            total   += len(labels)

        accuracy = correct / total
        return accuracy, loss
```

---

## 性能評価：端末満足度計算

**ファイル:** `cal.py:117–167`

### 調和平均満足度

```python
def calSatis(terms, aps):
    satis_sum_r = 0
    for term in terms:
        satis_r = calSatisTerm(term, aps)
        satis_sum_r += satis_r
    return len(terms) / satis_sum_r  # 調和平均
```

### 各端末の満足度

```python
def calSatisTerm(term, aps):
    apRtt = aps[term.apBssid].rtt   # 接続先APの現在RTT
    apTp  = aps[term.apBssid].tp    # 接続先APの現在TP
    TERM_APP = calAppNeed(term.appNum)

    if TERM_APP.indicator == 'tp':
        satis = apTp  / TERM_APP.needTP    # TP基準: 現在TP / 必要TP
    else:
        satis = TERM_APP.needRTT / apRtt   # RTT基準: 必要RTT / 現在RTT

    return 1 / satis  # 逆数（調和平均のため）
```

| 評価指標 | 計算式 | 意味 |
|---|---|---|
| TP満足度 | `AP_TP / App_needTP` | 1.0以上で要件充足 |
| RTT満足度 | `App_needRTT / AP_RTT` | 1.0以上で要件充足 |
| 調和平均 | `N / Σ(1/satis_i)` | システム全体の満足度指標 |

---

## 通信品質シミュレーション（cal.py）

接続端末数に応じてAPのTP・RTTが動的に劣化します。

### スループット計算（Erlang B 式）

```python
def erlang_b(lambda_val, mu, n):
    rho = lambda_val / mu
    numerator   = (rho ** n) / factorial(n)
    denominator = sum((rho ** k) / factorial(k) for k in range(n + 1))
    return numerator / denominator  # ブロック確率
```

### RTT計算（M/M/1 キューイング理論）

```python
def response_time_mm1(lambda_val, mu):
    if lambda_val >= mu - 1.9:          # 飽和防止クランプ
        lambda_val = mu - 1 + (lambda_val - mu) * 0.01
    return 1 / (mu - lambda_val)        # 平均応答時間
```

---

## 階層型連合学習（HFL）フロー

### 階層構造

```
セントラルサーバー (Layer N)
    ↑ 集約
中間サーバー群 (Layer N-1)   ← mid_server: [3] = 3台
    ↑ 集約
エッジサーバー群 (Layer 0)
    ↑ アップロード
端末（クライアント）群 (Layer -1)   ← 70台
```

### 学習ループ（`HFL_main.py:229–272`）

```
for round in range(args.epochs):
    端末をランダムに frac 割合でサンプリング
        ↓
    各端末でローカル学習（local_ep エポック）
        ↓
    重みをエッジサーバーにアップロード
        ↓
    エッジサーバーが重みを集約（FedAvg）
        ↓
    中間サーバーがエッジサーバーの重みを集約
        ↓
    セントラルサーバーが全体モデルを更新
        ↓
    トップダウン管理：性能良好な上位モデルを下位に配布
```

### コマンドライン引数（主要パラメータ）

```bash
python HFL_main.py \
  --epochs     10   \    # グローバル学習ラウンド数
  --num_users  70   \    # 端末（クライアント）数
  --frac       0.1  \    # 各ラウンドで参加する端末の割合
  --local_ep   10   \    # ローカルエポック数
  --local_bs   32   \    # ローカルバッチサイズ
  --lr         0.001\    # 学習率
  --optimizer  adam \    # オプティマイザ
  --model      cnn  \    # モデルタイプ（実際は3層MLPを使用）
  --dataset    terminal\ # データセット種別
  --num_classes 3   \    # 出力クラス数（基地局数）
  --iid        0    \    # 0=Non-IID, 1=IID
  --mid_server 3    \    # 中間サーバー数
  --download   True \    # クライアントがサーバーモデルをDLするか
  --management True      # トップダウンモデル管理を適用するか
```

---

## 実機研究への応用ポイント

### 1. リアルタイム推論への置き換え

現在のシミュレーションでは、CSVデータから端末の需要値を取得しています。実機では：

```python
# シミュレーション (現在)
np_tpNeed[item]  = TERMS[item].app.tpNeed
np_rttNeed[item] = TERMS[item].app.rttNeed

# 実機 (要置き換え)
np_tpNeed[item]  = get_realtime_tp_need(terminal_id)   # アプリ/OSから取得
np_rttNeed[item] = get_realtime_rtt_need(terminal_id)  # アプリ/OSから取得
```

### 2. モデルのポータビリティ

学習済みモデルは `global_model.state_dict()` で保存・ロード可能：

```python
# 保存
torch.save(global_model.state_dict(), 'model_hfl.pth')

# 実機でのロード・推論
model = Model(input_size=6, hidden_size=70, output_size=3)
model.load_state_dict(torch.load('model_hfl.pth'))
model.eval()

# 1端末分の推論例
x = torch.tensor([[norm_tp, norm_rtt, 1, 0, 0, 0]])  # appNum=0 のone-hot
with torch.no_grad():
    output = model(x)              # [1, 3]
    ap_id  = torch.argmax(output)  # 最適AP ID
```

### 3. 基地局数・アプリ種類の拡張

```python
# sim.json の変更のみで対応可能
{
  "apNumMax":  5,   # 基地局を5局に増設
  "appNumMax": 6    # アプリ種類を6種類に拡張
}
# モデルの output_size と input_size が自動的に調整される
```

### 4. 連合学習なしの集中型推論への変換

モデルアーキテクチャはそのまま、単一サーバーで学習して実機に配布する場合：

```python
# update.py の LocalUpdate.update() を標準のトレーニングループに置き換え
optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
criterion = nn.CrossEntropyLoss()

for epoch in range(num_epochs):
    for batch_x, batch_y in dataloader:
        optimizer.zero_grad()
        outputs = model(batch_x)
        loss    = criterion(outputs, batch_y)
        loss.backward()
        optimizer.step()
```

---

## 主要クラス・関数リファレンス

| 関数/クラス | ファイル | 役割 |
|---|---|---|
| `Model.forward()` | `HFL_main.py:152` | 推論の本体（3層MLP + Softmax） |
| `LocalUpdate.inference()` | `update.py:88` | テストデータでの精度・損失計算 |
| `LocalUpdate.update()` | `update.py:35` | ローカル学習（重み更新） |
| `average_weights()` | `utils.py` | FedAvg による重み集約 |
| `calSatis()` | `cal.py:117` | システム全体満足度（調和平均） |
| `calSatisTerm()` | `cal.py:136` | 端末個別の満足度計算 |
| `calLink()` | `cal.py:49` | 接続端末数→TP/RTT 変換 |
| `Structure` | `create_hierarchy.py` | 階層構造の管理 |
| `Term.setSwitchAp()` | `term.py` | 推論結果のAP割り当て適用 |

---

## データフロー全体図

```
[CSV / 実機センサー]
        ↓
  tpNeed, rttNeed, appNum
        ↓ z-score 正規化
  [norm_tp, norm_rtt]
        ↓ One-hot エンコード
  [norm_tp, norm_rtt, app0, app1, app2, app3]  → 6次元入力
        ↓
  ┌─────────────────────────────┐
  │  3層 MLP (PyTorch)          │
  │  Linear(6→70) + ReLU       │
  │  Linear(70→70) + ReLU      │
  │  Linear(70→3)  + Softmax   │
  └─────────────────────────────┘
        ↓
  [P(AP0), P(AP1), P(AP2)]  ← 確率分布
        ↓ argmax
  AP_ID ∈ {0, 1, 2}
        ↓ setSwitchAp()
  端末が最適APに接続
        ↓ calSatis()
  調和平均満足度でシステム評価
```

---

*本ドキュメントは `HFL_AP_selection-main` プロジェクトの推論フローを実機研究向けに整理した参考資料です。*
