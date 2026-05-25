# ハンガリアン法による教師データ作成 参考資料

> 実機研究向け参考プロンプト
> 対象ファイル: `hungarian_main.py` / `hungarian_kai.py`
> 作成日: 2026-04-27

---

## 概要

`hungarian_main.py` は **シミュレーションを繰り返しながらハンガリアン法で最適な基地局割り当てを求め、その結果を CSV に書き出す教師データ生成スクリプト** です。

生成された CSV（`70_event.csv` / `100_event.csv`）が、HFL モデルの学習データとなります。

```
hungarian_main.py   ←  教師データ生成スクリプト（単体実行）
      ↓ 生成
{termNum}_event.csv ←  教師データ（tpNeed, rttNeed, appNum, combiApTermArray）
      ↓ 読み込み
HFL_main.py         ←  学習・推論スクリプト
```

---

## 関連ファイル構成

| ファイル | 役割 |
|---|---|
| `hungarian_main.py` | メインスクリプト（ループ・CSV書き出し） |
| `hungarian_kai.py` | ハンガリアン法の実装（`call_hungarian` / `hungarian`） |
| `cal.py` | 通信品質計算・満足度計算 |
| `rand.py` | 端末・アプリのランダム初期化 |
| `create.py` | AP・端末インスタンス生成 |
| `sim.json` | シミュレーション設定（繰り返し回数・端末数など） |
| `app.json` | アプリケーション仕様（TP/RTT要件） |
| `ap.json` | 基地局仕様 |

---

## 教師データ生成フロー全体図

```
起動: python hungarian_main.py
        ↓
 AP・端末インスタンス生成（create.py）
        ↓
 ┌─────────────────────────────────────────┐
 │  for i in range(simNumTime):            │ ← simNumTime 回繰り返し
 │                                         │
 │  1. rand.randAp()                       │ ← 各端末にランダムAPを割り当て
 │  2. rand.randApp()                      │ ← 各端末にランダムアプリを割り当て
 │  3. cal.sumTermAp()                     │ ← AP毎の接続端末数をカウント
 │  4. cal.calLink()                       │ ← AP毎のTP/RTTを計算（劣化考慮）
 │  5. cal.calSatis() [割り当て前]          │ ← 割り当て前の満足度（記録用）
 │         ↓                              │
 │  6. hung.call_hungarian()               │ ← ハンガリアン法で最適割り当て決定
 │         ↓                              │
 │  7. combiApTermArray を記録             │ ← 正解ラベル収集
 │  8. tpNeed / rttNeed / appNum を記録   │ ← 特徴量収集
 │  9. cal.calSatis() [割り当て後]          │ ← 割り当て後の満足度（記録用）
 └─────────────────────────────────────────┘
        ↓
 CSV書き出し: [{termNum}_event.csv] に追記
 列: tpNeed, rttNeed, appNum, combiApTermArray
```

---

## ステップ詳細

### ステップ 1–2: ランダム初期化（`rand.py`）

```python
# 各端末にランダムで基地局 ID を割り当て
rand.randAp(TERMS, APS)
# → RAND_AP_NUM = floor(random() * len(aps))  ∈ {0, 1, 2}
# → term.setSwitchAp(RAND_AP_NUM)

# 各端末にランダムでアプリを割り当て（種類 + 利用時間）
rand.randApp(TERMS, APS)
# → RAND_APP_NUM = floor(random() * APP_NUM_MAX)  ∈ {0, 1, 2, 3}
# → USE_TIME = floor(random() * (MAX_TIME - MIN_TIME + 1) + MIN_TIME)
# → term.setAppNum(RAND_APP_NUM, USE_TIME)
```

**この時点では AP 割り当てはランダム（非最適）。次のハンガリアン法で最適化する。**

---

### ステップ 3–4: 通信品質計算（`cal.py`）

#### `sumTermAp()` — AP毎の接続端末数カウント

```python
def sumTermAp(terms, aps):
    apTermNum = np.zeros(len(aps))
    for term in terms:
        apTermNum[term.apBssid] += 1   # 各AP毎に端末数をカウント
    for index, ap in enumerate(aps):
        ap.setTermNum(apTermNum[index])
    return apTermNum
```

#### `calLink()` — 接続端末数に応じたTP/RTT計算

```python
def calLink(terms, aps, sec):
    # 初期値（AP0, AP1, AP2）
    init_rtt = [20.0, 28.0, 32.0]            # 初期RTT [ms]
    init_tp  = [65500*2*8/rtt/1024 for rtt in init_rtt]  # 初期TP [Mbps]

    # Erlang B（TP劣化）パラメータ
    erlang_mu = [50, 50, 20]   # サービス率
    erlang_n  = [10,  5,  3]   # 回線数

    # M/M/1（RTT劣化）パラメータ
    respo_mu = [1/rtt*1000 for rtt in init_rtt]

    apTermNum = sumTermAp(terms, aps)

    for i in range(len(aps)):
        # TP: Erlang B で輻輳によるブロック率を算出し劣化
        erlang    = erlang_b(apTermNum[i]*100, erlang_mu[i], erlang_n[i])
        link_tp[i] = init_tp[i] * (1 - erlang)

        # RTT: M/M/1 キューイング理論で応答遅延を算出
        link_rtt[i] = response_time_mm1(apTermNum[i], respo_mu[i]) * 1000

    for index, ap in enumerate(aps):
        ap.setRtt(link_rtt[index])
        ap.setTp(link_tp[index])
```

| AP | 初期RTT | 初期TP概算 | Erlang µ | Erlang n | M/M/1 µ |
|---|---|---|---|---|---|
| AP0 | 20 ms | ~51 Mbps | 50 | 10 | 50 /s |
| AP1 | 28 ms | ~37 Mbps | 50 | 5  | ~36 /s |
| AP2 | 32 ms | ~32 Mbps | 20 | 3  | ~31 /s |

---

### ステップ 6: ハンガリアン法（`hungarian_kai.py`）

#### `call_hungarian(terms, aps)` — 最適AP割り当ての決定

```
全組み合わせを列挙（makeCombiApTerm）
        ↓
各組み合わせについて：
  コスト行列（満足度行列）を構築
        ↓
  ハンガリアン法で最適マッチングを求める（hungarian）
        ↓
  割り当て後の調和平均満足度を算出
        ↓
最大調和平均の組み合わせを選択
  → 同値の場合は最小満足度が最大の組み合わせを優先
        ↓
全端末に最適AP IDをセット（setSwitchAp）
```

#### `makeCombiApTerm(terms, aps)` — 全組み合わせ列挙

**仕切り（Partition）法** を用いて、端末を基地局へ割り振る全パターンを生成します。

```python
def makeCombiApTerm(terms, aps):
    n = len(terms) + len(aps) - 1   # 例: 70端末 + 3AP → n=72
    r = len(aps) - 1                # 仕切り数 = AP数-1 = 2

    # [0, 1, 2, ..., n-1] から r個の仕切り位置を選ぶ組み合わせを生成
    division = kumiawase(data, r)

    # 仕切りの位置からAP番号配列に変換
    # 例: 仕切り位置 [3, 7] → 端末0-2がAP1, 端末3-6がAP2, 端末7以降がAP3
    ...
    return combi_ap_term_tmp  # shape: [パターン数, 端末数]
```

> **注意:** 端末数70の場合、組み合わせ数は非常に大きくなります（`C(72,2) = 2556` パターン）。

#### コスト行列の構築（`call_hungarian` 内）

```python
costMatrix = np.zeros((len(TERMS_VIRTUAL), len(TERMS_VIRTUAL)))  # 正方行列 [端末数×端末数]

for i in range(len(COMBI_AP_TERM)):           # 各組み合わせパターンについて
    for j in range(len(terms)):               # 行: 基地局リソース（端末数と同数）
        distAp = COMBI_AP_TERM[i][j] - 1     # j番目のリソースが属するAP ID
        TERMS_VIRTUAL[k].setSwitchAp(distAp) # 仮想的に接続先をセット

        for k in range(len(terms)):           # 列: 端末
            satis = cal.calSatisTerm_a(TERMS_VIRTUAL[k], APS_VIRTUAL)
            costMatrix[j][k] = round(satis, 6)
```

**コスト行列の意味：** `costMatrix[j][k]` = 端末kが基地局リソースjに割り当てられたときの満足度

#### `calSatisTerm_a()` — コスト行列用の満足度計算

```python
def calSatisTerm_a(term, aps):
    apRtt = aps[term.apBssid].rtt
    apTp  = aps[term.apBssid].tp
    TERM_APP = calAppNeed(term.appNum)

    if TERM_APP.indicator == 'tp':
        satis   = apTp / TERM_APP.needTP       # TP基準: 現在TP / 必要TP
        satis_r = TERM_APP.needTP / apTp       # 逆数（調和平均用）
    else:
        satis   = TERM_APP.needRTT / apRtt     # RTT基準: 必要RTT / 現在RTT
        satis_r = apRtt / TERM_APP.needRTT     # 逆数（調和平均用）

    return satis   # ← 満足度そのものを返す（コスト行列に格納する値）
```

> **`calSatisTerm` との違い:**
> - `calSatisTerm` → `1/satis` を返す（評価用・調和平均計算で使う逆数）
> - `calSatisTerm_a` → `satis` を返す（コスト行列に入れる値。大きいほど良い）

#### `hungarian(costMatrix, combi_ap_term)` — ハンガリアン法本体

```python
def hungarian(costMatrix, combi_ap_term):
    N = len(costMatrix[0])

    # float → int 変換（精度確保: × 10^6）
    b[i][j] = floor(costMatrix[i][j] * FIX_DIGIT)

    # ラベリング法（最大重みマッチング）
    fx = [max(b[i]) for each row i]   # 行ラベル（初期値: 各行の最大値）
    fy = [0 for each col j]           # 列ラベル（初期値: 0）
    x  = [-1 for each row i]          # 行→列のマッチング
    y  = [-1 for each col j]          # 列→行のマッチング

    # 増加路探索 → 実行可能な等式グラフ上でマッチングを拡大
    # マッチングが完成しない場合はラベルを更新（d を引く）して再探索
    ...

    # 結果の変換: マッチング座標 x[i] → AP番号配列
    for i, new_index in enumerate(x):
        wk_solution_station[new_index] = combi_ap_term[i]

    # 調和平均・合計・最小値の算出
    RESULT.Harmean = N / sum(1/satis for each matched pair)
    RESULT.Sum     = sum(satis for each matched pair)
    RESULT.min     = min(satis for each matched pair)
    RESULT.combiApTermArray = wk_solution_station   # 各端末のAP ID配列

    return RESULT
```

#### 最適組み合わせの選択基準

```python
# 1. 調和平均が最大の組み合わせを選ぶ
index_of_max = max(enumerate(hungarianResultAll), key=lambda x: x[1].Harmean)[0]
HUNGARIAN_MAX_VALUE = hungarianResultAll[index_of_max].Harmean

# 2. 調和平均が同値の場合は、最小満足度が最大（最悪端末を救う）のものを優先
resArray = [res for res in hungarianResultAll if res.Harmean >= HUNGARIAN_MAX_VALUE]
res = max(resArray, key=lambda r: r.min)
```

---

### ステップ 7–8: 特徴量・正解ラベルの収集（`hungarian_main.py:83–87`）

```python
# ハンガリアン法の結果（正解ラベル）を収集
for com in combination.combiApTermArray:
    combi.append(com)   # ← 各端末の最適AP ID（0/1/2）

# 特徴量を収集
for term in TERMS:
    appNum.append(term.appNum)           # アプリ番号
    app = cal.calAppNeed(term.appNum)
    tpNeed.append(app.needTP)            # 必要スループット
    rttNeed.append(app.needRTT)          # 必要遅延時間
```

---

### ステップ 9: CSV 書き出し（`hungarian_main.py:148–161`）

```python
file = []
for item in range(len(tpNeed)):
    row = [tpNeed[item], rttNeed[item], appNum[item], combi[item]]
    file.append(row)

file_name = str(confSim['termNum']) + '_event.csv'   # 例: "70_event.csv"
with open(file_name, 'a') as f:          # 'a' = 追記モード
    writer_object = writer(f)
    for item in file:
        writer_object.writerow(item)
```

---

## 生成される教師データ（CSV）

### フォーマット

| 列 | 型 | 内容 |
|---|---|---|
| `tpNeed` | float | 端末が必要とするスループット [Mbps]（0 or アプリ定義値） |
| `rttNeed` | float | 端末が必要とする遅延時間 [ms]（0 or アプリ定義値） |
| `appNum` | int | 利用アプリ番号（0〜3） |
| `combiApTermArray` | int | **正解ラベル：ハンガリアン法が決定した最適AP ID（0〜2）** |

### 各アプリと tpNeed / rttNeed の対応

| appNum | appType | indicator | tpNeed | rttNeed |
|---|---|---|---|---|
| 0 | ブラウザ | tp | 5 | 0 |
| 1 | 動画 | tp | 10 | 0 |
| 2 | 通話 | rtt | 0 | 200 |
| 3 | 配信 | rtt | 0 | 100 |

> TP基準アプリは `rttNeed=0`、RTT基準アプリは `tpNeed=0` となる。
> これがモデルの入力特徴量として One-hot + 正規化で変換される。

### データ量の計算

```
1回のシミュレーション = termNum 行分のデータが追記
termNum=70, simNumTime=10 → 700行/実行
70_event.csv の場合: 8750行 = 125回実行分
```

---

## コスト行列と最適化の数学的整理

```
コスト行列 C（N×N 正方行列, N=端末数=70）

         端末0   端末1   ...  端末N-1
AP_res0  [s00,   s01,   ..., s0(N-1)]
AP_res1  [s10,   s11,   ..., s1(N-1)]
  :
AP_res(N-1) [...]

sij = 端末j が AP_resource_i に割り当てられたときの満足度

目的: Σ sij を最大化するマッチング（各行・各列から1つずつ選ぶ）
```

**ハンガリアン法の計算量:** O(N³) = O(70³) ≈ 343,000 演算（1組み合わせあたり）

---

## `calSatisTerm` vs `calSatisTerm_a` の比較

| 関数 | 返り値 | 使用箇所 | 目的 |
|---|---|---|---|
| `calSatisTerm` | `1/satis`（逆数） | `calSatis()` → 評価 | 調和平均算出用（逆数の和→N/和） |
| `calSatisTerm_a` | `satis`（満足度） | `call_hungarian()` → コスト行列 | ハンガリアン法の最大化問題に入力 |

---

## 実機研究への応用ポイント

### 1. 教師データ生成のパラメータ調整

```json
// sim.json
{
  "termNum": 70,       // 端末数を変更 → モデル hidden_size が変わる
  "simNumTime": 10,    // 増やすほど多くのデータが生成される（1回=70行）
  "apNumMax": 3        // 基地局数を変更 → モデル output_size が変わる
}
```

### 2. CSV追記モードに注意

`hungarian_main.py` は `open(file_name, 'a')` で **追記** します。
実験を繰り返すと既存の CSV にデータが足されます。新規データのみにしたいときは先に CSV を空にしてから実行してください。

### 3. ハンガリアン法の選択基準の変更

現在の選択基準：
```
1. 調和平均最大（システム全体の効率）
2. 同値時は最小満足度最大（最悪端末を救う）
```

選択基準を変更する場合は `hungarian_kai.py:253–288` を修正します。

### 4. 端末数増加時の計算量

組み合わせ数 = `C(termNum + apNumMax - 1, apNumMax - 1)` で増大します。

| termNum | apNumMax | 組み合わせ数 |
|---|---|---|
| 10 | 3 | 66 |
| 70 | 3 | 2,556 |
| 100 | 3 | 5,151 |

端末数が増加すると実行時間が大きく増加します。実機では近似手法への置き換えも検討してください。

---

## 実行方法

```bash
# 教師データ生成（ハンガリアン法）
python hungarian_main.py
# → {termNum}_event.csv に追記

# 生成データを使った HFL 学習・推論
python HFL_main.py \
  --epochs 10 \
  --num_users 70 \
  --local_ep 10 \
  --local_bs 32 \
  --lr 0.001 \
  --optimizer adam \
  --dataset terminal \
  --num_classes 3
```

---

*本ドキュメントは `hungarian_main.py` / `hungarian_kai.py` における教師データ生成フローを実機研究向けに整理した参考資料です。*
