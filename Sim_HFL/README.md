# Sim_HFL — HFL シミュレーション環境

`HFL_terminals` / `Serverside_HFL` と並列して配置される、
**Android・サーバ不要**のフェデレーテッドラーニング純 Python シミュレーターです。

## 目的

| 比較対象 | 説明 |
|---|---|
| **実験値 vs 理論値** | 本シミュレーターで得た収束曲線を FedAvg 理論上界と比較 |
| **IID vs non-IID** | データ分布の違いが精度・収束速度に与える影響を定量化 |
| **通信コスト分析** | ラウンド数・端末数による通信量の理論的コストを算出 |

---

## フォルダ構成

```
Sim_HFL/
├── run_sim.py            # メインエントリポイント
├── requirements.txt      # Python 依存ライブラリ
├── config/
│   └── default.yaml      # シミュレーション設定
├── sim/                  # コアシミュレーションコード
│   ├── model.py          # AP選択 MLP（実機と同一アーキテクチャ）
│   ├── data_gen.py       # 合成データ生成・IID/non-IID 分割
│   ├── terminal.py       # 仮想端末（実際に PyTorch で学習）
│   ├── aggregator.py     # FedAvg 集約器
│   └── runner.py         # FL ループオーケストレーター
├── theory/
│   └── fedavg_bounds.py  # FedAvg 収束理論上界（Li et al. 2020）
├── analysis/
│   └── compare.py        # シミュレーション vs 理論値 比較レポーター
├── results/
│   ├── logs/             # ラウンドごとの JSONL ログ（自動生成）
│   └── reports/          # 比較レポート・グラフ（自動生成）
└── data/                 # 将来的な実データ配置用（現在は空）
```

---

## セットアップ

```bash
cd Sim_HFL
pip install -r requirements.txt
```

---

## 使い方

### 基本実行（IID、5端末、30ラウンド）

```bash
python run_sim.py
```

### 理論値比較レポートも同時生成

```bash
python run_sim.py --compare
```

### 設定を上書きして実行

```bash
# non-IID、10端末、50ラウンド、理論値比較あり
python run_sim.py --no-iid --terminals 10 --rounds 50 --epochs 5 --compare
```

### 既存ログから理論値比較のみ実行

```bash
python run_sim.py --compare-only results/logs/rounds_20240101_000000_abc12345.jsonl
```

---

## 主要オプション

| オプション | 説明 | デフォルト |
|---|---|---|
| `--rounds N`     | 通信ラウンド数 | 30 |
| `--terminals N`  | 仮想端末数     | 5  |
| `--epochs N`     | ローカルエポック数 | 5 |
| `--lr F`         | 学習率          | 0.01 |
| `--no-iid`       | non-IID 分割を使用 | IID |
| `--alpha F`      | ディリクレ α（non-IID の偏り強度） | 0.5 |
| `--samples N`    | 合計サンプル数    | 5000 |
| `--compare`      | 理論値比較レポートを生成 | off |
| `--no-plot`      | グラフ生成をスキップ | off |

---

## 理論的背景

### FedAvg 収束理論（Li et al. 2020）

**仮定**: 損失関数が L-smooth かつ μ-strongly convex、勾配ノイズ分散が σ²

**IID の場合**:
```
E[F(w_T)] - F(w*) ≤ (2·L·σ²) / (μ²·K·E·T)
```

**non-IID の場合**（異質性バイアス Γ が追加）:
```
E[F(w_T)] - F(w*) ≤ (2·L·σ²) / (μ²·K·E·T)  +  Γ
```

ここで:
- T = 通信ラウンド数
- K = 参加端末数  
- E = ローカルエポック数
- Γ = `(6·E·η·L·G²) / μ`（データ異質性による残留バイアス）

---

## 出力ファイル

| ファイル | 内容 |
|---|---|
| `results/logs/rounds_*.jsonl` | ラウンドごとの端末学習・集約イベント（実機ログと同一スキーマ） |
| `results/reports/*_comparison.jsonl` | ラウンドごとの実験値 vs 理論値の差分 |
| `results/reports/*_summary.json`    | 全体サマリー（収束ラウンド・通信コスト等） |
| `results/reports/*_plot.png`        | 収束曲線グラフ（Accuracy / Loss） |

---

## 実機との連携

`results/logs/` の JSONL スキーマは `HFL_terminals` の `RealTimeLogger` 出力と同一の
`schema_version`, `ts_ms`, `run_id`, `terminal_id`, `round_id` フィールドを持ちます。
実機実験ログと並べて分析することが可能です。
