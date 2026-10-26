# ハンガリアン法による教師データ作成 ガイド

> 対象ファイル: `hungarian_main.py` / `hungarian_kai.py`
> 作成日: 2026-05-12

---

## 1. 概要

`hungarian_main.py` は、**ハンガリアン法（Hungarian Algorithm）** を用いて各端末を最適な基地局（AP）に割り当て、その結果を CSV ファイルに書き出すことで **HFL モデルの教師データを生成するスクリプト** です。

生成された CSV ファイルは `HFL_main.py` から読み込まれ、AP 選択モデルの学習に使用されます。

```
python hungarian_main.py
        ↓ 生成
{termNum}_event.csv   ← 教師データ（特徴量 + 正解ラベル）
        ↓ 読み込み
python HFL_main.py    ← HFL モデルの学習・推論
```

---

## 2. 関連ファイル構成

| ファイル | 役割 |
|---|---|
| `hungarian_main.py` | メインスクリプト（対話式設定・ループ処理・CSV書き出し） |
| `hungarian_kai.py` | ハンガリアン法の実装（全組み合わせ列挙・コスト行列構築・最適割り当て） |
| `cal.py` | 通信品質計算（TP/RTT）・満足度計算 |
| `rand.py` | 端末へのランダム AP・アプリ割り当て |
| `create.py` | AP・端末インスタンスの生成 |
| `term.py` | 端末（Terminal）のデータ構造 |
| `ap.py` | 基地局（Access Point）のデータ構造 |
| `sim.json` | シミュレーション設定（端末数・繰り返し回数など） |
| `app.json` | アプリケーション仕様（TP/RTT 要件） |
| `ap.json` | 基地局仕様（データ上限・価格など） |

---

## 3. 入力

### 3-1. 対話式入力（起動時プロンプト）

スクリプト起動後、以下のパラメータをキーボードで指定します。  
何も入力せずに Enter を押すとカッコ内のデフォルト値が使用されます。

| 項目 | 変数名 | デフォルト値 | 説明 |
|---|---|---|---|
| 端末台数 | `term_num` | `sim.json["termNum"]` = 70 | シミュレーションする端末の数 |
| 基地局数 | `ap_num` | `sim.json["apNumMax"]` = 3 | 使用する AP の数 |
| AP0 初期RTT | `init_rtt[0]` | 20.0 ms | AP0 の基準応答遅延 |
| AP0 回線数 | `erlang_n[0]` | 10 | AP0 の Erlang B モデルの回線数 |
| AP1 初期RTT | `init_rtt[1]` | 28.0 ms | AP1 の基準応答遅延 |
| AP1 回線数 | `erlang_n[1]` | 5 | AP1 の Erlang B モデルの回線数 |
| AP2 初期RTT | `init_rtt[2]` | 32.0 ms | AP2 の基準応答遅延 |
| AP2 回線数 | `erlang_n[2]` | 3 | AP2 の Erlang B モデルの回線数 |
| 目標行数 | `target_rows` | `term_num × simNumTime` | CSV に書き出す合計行数 |

> **再開機能:** 出力先 CSV が既に存在する場合、既存行数を読み取り、不足分だけ追加生成して目標行数に達するまで実行します。

### 3-2. 設定ファイル（JSON）

**sim.json**
```json
{
  "termNum":    70,   // デフォルト端末数
  "apNumMax":   3,    // デフォルト基地局数
  "appNumMax":  4,    // アプリ種類数
  "appUseSec":  90,   // アプリ利用時間（秒）—— calLink の sec パラメータ
  "simNumTime": 10    // 1実行あたりの繰り返し回数（1回 = term_num 行）
}
```

**app.json**（アプリケーション仕様）

| appNum | appType | indicator | needTP [Mbps] | needRTT [ms] | 利用時間 |
|---|---|---|---|---|---|
| 0 | ブラウザ | tp | 5 | 0（無効） | 60〜120 秒 |
| 1 | 動画 | tp | 10 | 0（無効） | 3600〜6000 秒 |
| 2 | 通話 | rtt | 0（無効） | 200 | 30〜60 秒 |
| 3 | 配信 | rtt | 0（無効） | 100 | 60〜6000 秒 |

> `indicator` が `tp` のアプリは TP（スループット）で満足度を評価し、`rtt` のアプリは RTT（遅延）で評価します。

**ap.json**（基地局仕様）

| AP ID | dataLimit [MB] | price [円] |
|---|---|---|
| 0 | 7168 | 7000 |
| 1 | 5120 | 5000 |
| 2 | 3072 | 3000 |

---

## 4. 処理フロー全体図

```
python hungarian_main.py
          ↓
  [対話式設定]
  term_num, ap_num, init_rtt, erlang_n, target_rows を入力
          ↓
  AP インスタンス生成（create.createAp）
  端末インスタンス生成（create.createTerm）
          ↓
  ┌──────────────────────────────────────────────────────────┐
  │  while written_rows < remaining:  ← 目標行数に達するまで │
  │                                                          │
  │  Step 1: rand.randAp()   ← 各端末にランダムで AP を割り当て│
  │  Step 2: rand.randApp()  ← 各端末にランダムでアプリを割り当て│
  │  Step 3: cal.sumTermAp() ← AP 毎の接続端末数を集計       │
  │  Step 4: cal.calLink()   ← AP 毎の TP/RTT を計算         │
  │  Step 5: cal.calSatis()  ← 割り当て前の満足度を計算（表示用）│
  │          ↓                                               │
  │  Step 6: hung.call_hungarian()                           │
  │          ├─ makeCombiApTerm()  全割り当てパターンを列挙    │
  │          ├─ コスト行列構築（端末 × AP リソース）           │
  │          ├─ hungarian()        最大重みマッチングを計算    │
  │          └─ 調和平均最大・最小満足度最大の組み合わせを選択  │
  │          ↓                                               │
  │  Step 7: cal.sumTermAp(), cal.calLink(), cal.calSatis()  │
  │          ← 割り当て後の TP/RTT・満足度を再計算            │
  │  Step 8: CSV に 1 ラウンド分（term_num 行）を追記         │
  └──────────────────────────────────────────────────────────┘
          ↓
  {term_num}_event.csv  ← 教師データ完成
```

---

## 5. ステップ詳細

### Step 1: ランダム AP 割り当て（`rand.randAp`）

各端末に対して、利用可能な AP の中からランダムで接続先を割り当てます。  
この時点での割り当ては暫定的（非最適）であり、Step 6 のハンガリアン法で最適化されます。

```python
RAND_AP_NUM = math.floor(random.random() * len(aps))  # 0, 1, 2 のいずれか
term.setSwitchAp(RAND_AP_NUM)
```

### Step 2: ランダムアプリ割り当て（`rand.randApp`）

各端末に対して、アプリ種類と利用時間をランダムで決定します。

```python
RAND_APP_NUM = math.floor(random.random() * APP_NUM_MAX)   # 0〜3 のいずれか
USE_TIME     = math.floor(random.random() * (MAX_TIME - MIN_TIME + 1) + MIN_TIME)
term.setAppNum(RAND_APP_NUM, USE_TIME)
```

### Step 3: 接続端末数の集計（`cal.sumTermAp`）

各 AP に何台の端末が接続しているかをカウントし、AP インスタンスに保存します。

```python
apTermNum = np.zeros(len(aps))
for term in terms:
    apTermNum[term.apBssid] += 1
for index, ap in enumerate(aps):
    ap.setTermNum(apTermNum[index])
```

### Step 4: TP / RTT の計算（`cal.calLink`）

接続端末数に基づいて、**2 種類のキューイングモデル** で各 AP の通信品質を算出します。

#### TP の劣化 — Erlang B モデル（回線混雑）

$$
B = \frac{\rho^n / n!}{\sum_{k=0}^{n} \rho^k / k!}, \quad \rho = \frac{\lambda}{\mu}
$$

- $\lambda$: 到着率（接続端末数 × 100）
- $\mu$: サービス率（固定値）
- $n$: 回線数（`erlang_n`、対話式で設定）
- $B$: ブロック率 → TP = 初期 TP × (1 − B)

#### RTT の劣化 — M/M/1 モデル（応答遅延）

$$
W = \frac{1}{\mu - \lambda}
$$

- $\mu = 1 / \text{初期RTT} \times 1000$
- $\lambda$: 接続端末数
- $W$: 平均応答時間（秒）→ RTT = W × 1000 ms

| AP | 初期RTT | 初期TP概算 | Erlang µ | Erlang n（デフォルト） |
|---|---|---|---|---|
| AP0 | 20 ms | ~51 Mbps | 50 | 10 |
| AP1 | 28 ms | ~37 Mbps | 50 | 5 |
| AP2 | 32 ms | ~32 Mbps | 20 | 3 |

### Step 5: 割り当て前の満足度計算（`cal.calSatis`）

現時点の（ランダム割り当て後の）満足度を計算して表示します（記録・比較用）。

端末 1 台の満足度は以下で定義されます：

$$
s_i =
\begin{cases}
\dfrac{\text{AP の TP}}{\text{必要 TP}} & \text{（indicator = tp のアプリ）} \\[8pt]
\dfrac{\text{必要 RTT}}{\text{AP の RTT}} & \text{（indicator = rtt のアプリ）}
\end{cases}
$$

全端末の総合満足度は**調和平均**で評価します：

$$
H = \frac{N}{\sum_{i=1}^{N} \frac{1}{s_i}}
$$

### Step 6: ハンガリアン法による最適割り当て（`hung.call_hungarian`）

#### 6-1. 全割り当てパターンの列挙（`makeCombiApTerm`）

「仕切り法（Partition）」を使って、N 台の端末を K 個の AP に分割する全パターンを生成します。

$$
\text{パターン数} = \binom{N + K - 1}{K - 1}
$$

例：端末 70 台・AP 3 個 → $\binom{72}{2} = 2{,}556$ パターン  
例：端末 100 台・AP 3 個 → $\binom{102}{2} = 5{,}151$ パターン

各パターンは「端末 i が AP j に接続する」という配列（長さ N）で表現されます。

#### 6-2. コスト行列の構築

各パターンについて、$N \times N$ の正方コスト行列を構築します。

$$
C[j][k] = \text{端末 } k \text{ が AP リソース } j \text{ に割り当てられたときの満足度 } s_{jk}
$$

- 行（j）: AP リソース（仮想的に端末数と同数に拡張）
- 列（k）: 端末
- 値: `calSatisTerm_a` で計算した満足度（大きいほど良い）

コスト行列構築の際、対象パターンの割り当てに基づいて `calLink` を再計算することで、**接続台数の変化に伴う TP/RTT の変動も反映** しています。

#### 6-3. ハンガリアン法本体（`hungarian`）

scipy の `linear_sum_assignment`（最大重みマッチング）を用いて、コスト行列の各行・各列から 1 つずつ選ぶことで **満足度の合計が最大になるマッチング** を決定します。

```
コスト行列 C（N×N）
→ linear_sum_assignment（最大化）
→ 各端末に AP リソースを 1 対 1 で割り当てるマッチング
→ 調和平均・満足度合計・最小満足度を算出
```

> **実装上の注意:** float → int 変換（× 10⁶）を行い、精度を確保しています。

#### 6-4. 最適パターンの選択基準

全パターンのハンガリアン法結果から、以下の優先順位で最適パターンを選択します。

1. **調和平均が最大** のパターンを選ぶ（システム全体の効率を最大化）
2. 同値が複数ある場合は、**最小満足度が最大**（最も不満な端末を救う）のパターンを優先

```python
# 1. 調和平均最大を特定
HUNGARIAN_MAX_VALUE = max(res.Harmean for res in hungarianResultAll)

# 2. 同値の中で最小満足度最大を選択
resArray = [res for res in hungarianResultAll if res.Harmean >= HUNGARIAN_MAX_VALUE]
res = max(resArray, key=lambda r: r.min)
```

最適パターンが決定したら、全端末の `apBssid`（接続 AP ID）を最適値に更新します。

### Step 7: 割り当て後の TP / RTT・満足度の再計算

最適割り当て後の状態で `sumTermAp` → `calLink` → `calSatis` を再実行し、割り当て後の通信品質を確定します。

### Step 8: CSV への書き出し

1 ラウンド分（`term_num` 行）のデータを CSV ファイルに**追記**（`'a'` モード）します。

```python
with open(file_name, 'a', newline='') as f:
    csv_writer = writer(f)
    for term in TERMS:
        app = cal.calAppNeed(term.appNum)
        ap_id = int(combination.combiApTermArray[term.id])
        csv_writer.writerow([app.needTP, app.needRTT, term.appNum, ap_id])
```

---

## 6. 出力

### 出力ファイル

| 項目 | 内容 |
|---|---|
| ファイル名 | `{term_num}_event.csv`（例: `70_event.csv`、`100_event.csv`） |
| 書き込みモード | 追記（`'a'`） |
| 1 行の意味 | 端末 1 台の 1 ラウンド分のデータ |
| 1 ラウンドの行数 | `term_num` 行（例: 端末 70 台 → 70 行/ラウンド） |

### CSV 列定義

| 列インデックス | 列名 | 型 | 説明 |
|---|---|---|---|
| 0 | `tpNeed` | float | 端末が必要とする TP [Mbps]（TP 基準アプリのみ、RTT 基準アプリは 0） |
| 1 | `rttNeed` | float | 端末が必要とする RTT [ms]（RTT 基準アプリのみ、TP 基準アプリは 0） |
| 2 | `appNum` | int | 利用アプリ番号（0〜3） |
| 3 | `combiApTermArray` | int | **正解ラベル**: ハンガリアン法が決定した最適 AP ID（0〜2） |

### CSV サンプル

```
# tpNeed, rttNeed, appNum, AP_ID（正解ラベル）
5.0, 0, 0, 2
10.0, 0, 1, 0
0, 200.0, 2, 1
0, 100.0, 3, 0
5.0, 0, 0, 1
```

### データ量の目安

```
1 ラウンド  = term_num 行
1 実行      = simNumTime × term_num 行

例: term_num=70, simNumTime=10
→ 1 実行 = 700 行
→ 8,750 行（= 125 実行分）の CSV を作成する場合、125 回実行が必要
```

---

## 7. 停止と再開

### 途中停止

実行中に以下のいずれかで停止できます。現在のラウンド完了後に安全に終了します。

- `Ctrl+C` を押す
- ターミナルに `q` を入力して Enter を押す

### 再開

同じ `term_num` で再実行すると、既存 CSV の行数を読み取り、**不足分だけ追加生成**して再開します。

```
既存行数 = 2100 行
目標行数 = 3500 行
→ 追加生成 = 1400 行（= 20 ラウンド分）
```

---

## 8. 実行方法

```bash
cd /path/to/HFL_AP_selection-main
source .venv/bin/activate

# 教師データ生成
python hungarian_main.py

# プロンプト例:
# 端末台数          (デフォルト: 70):          ← Enter でデフォルト使用
# 基地局数          (デフォルト: 3):            ← Enter でデフォルト使用
#   AP0 初期RTT [ms]  (デフォルト: 20.0):       ← Enter でデフォルト使用
#   AP0 回線数 erlang_n (デフォルト: 10):        ← Enter でデフォルト使用
#   AP1 初期RTT [ms]  (デフォルト: 28.0):       ← Enter でデフォルト使用
#   AP1 回線数 erlang_n (デフォルト: 5):         ← Enter でデフォルト使用
#   AP2 初期RTT [ms]  (デフォルト: 32.0):       ← Enter でデフォルト使用
#   AP2 回線数 erlang_n (デフォルト: 3):         ← Enter でデフォルト使用
# 目標行数          (デフォルト: 700):           ← Enter でデフォルト使用
```

---

## 9. 注意事項・よくある問題

### CSV の追記モードに注意

`open(file_name, 'a')` で追記するため、**異なる設定での実行結果が同じファイルに混在する** ことがあります。新しいデータセットを作り直す場合は、先に CSV を削除または空にしてから実行してください。

```bash
# 既存データを削除してから再生成する場合
rm 70_event.csv
python hungarian_main.py
```

### 端末数増加時の計算時間

組み合わせ数は端末数に対して急速に増加します。端末数が多い場合は実行時間が大幅に増加します。

| 端末数 | AP 数 | 組み合わせ数 |
|---|---|---|
| 10 | 3 | 66 |
| 70 | 3 | 2,556 |
| 100 | 3 | 5,151 |
| 70 | 4 | 27,405 |

### sim.json の設定変更

端末数・基地局数を変更した場合、HFL モデルのアーキテクチャ（`hidden_size`・`output_size`）も変更が必要です。

```json
// sim.json の主要パラメータ
"termNum":    70,   // → モデルの入力次元に影響
"apNumMax":   3,    // → モデルの出力次元（クラス数）に影響
"simNumTime": 10    // → 1 実行あたりの生成行数に影響
```

---

## 10. ファイル間の依存関係まとめ

```
hungarian_main.py
├── create.py           AP・端末インスタンス生成
├── rand.py             ランダム AP・アプリ割り当て
├── cal.py              TP/RTT・満足度計算
│   ├── app.json        アプリ要件定義
│   └── sim.json        シミュレーション設定
├── hungarian_kai.py    ハンガリアン法実装
│   ├── cal.py          (TP/RTT 計算を内部で呼び出す)
│   ├── app.json
│   └── sim.json
├── term.py             端末データ構造
├── ap.py               AP データ構造
├── sim.json
├── ap.json             AP スペック定義
└── app.json
```

---

*本ドキュメントは `hungarian_main.py` / `hungarian_kai.py` の実装をもとに、教師データ生成の手順・入出力・内部アルゴリズムを詳細にまとめたものです。*
