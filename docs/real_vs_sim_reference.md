# HFL 実機 vs シミュレーション連携リファレンス (AIエージェント向け指示書)

このドキュメントは、Pythonベースのシミュレータ (`Sim_HFL`) を、Android実機およびエッジ/中央サーバ群 (`HFL_terminals` / `Serverside_HFL`) の実際の処理ロジックに可能な限り近づけるための改修要件と参考資料です。

他のAIエージェントが `Sim_HFL` のコード（特に `sim/terminal.py`, `sim/aggregator.py`, `run_sim.py`）を修正する際、**このドキュメントをSingle Source of Truth (SSOT)** として扱ってください。

---

## 1. 全体アーキテクチャ (Multi-tier Federated Learning)

### 実機の挙動
- **3層構造:** Terminal (Android) -> Edge Server (FastAPI) -> Central Server (FastAPI)。
- **集約のタイミング:** 
  1. Terminalがローカル学習を終え、接続先のEdgeへモデルをアップロードする。
  2. Edgeは事前に定められた閾値（例: 接続端末数の80%）に達するとローカル集約（Edge Aggregation）を実行し、Centralへ送信する。
  3. Centralは各Edgeからの更新を待って全体集約（Global Aggregation）を行う。

### シミュレータ (`Sim_HFL`) の要件
- 既存の `run_sim.py` はEdgeトポロジを定義していますが、`sim/aggregator.py` が単純な1段階のGlobal FedAvgになっていないか確認してください。
- **改修指示:** `FedAvgAggregator` を拡張し、`Edge` クラスを導入して「端末群の加重平均」→「エッジ群の加重平均」の2段階集約を忠実に模倣する（あるいは、既存の実装が既にそうであれば、実機の `aggregation_threshold` などの概念を導入して遅延シミュレーションを強化する）こと。

---

## 2. ローカル学習ロジック (Terminal)

シミュレータの `sim/terminal.py` は既に実機のKotlin実装(`LocalTrainer.kt`)の一部を模倣していますが、以下の点が完全一致しているか確認・修正してください。

### A. オプティマイザとスケジューラ
- **AdamW:** `weight_decay=0.01`。ただし、**LayerNorm の weight/bias は `weight_decay=0.0` に分離**する（Kotlinの `isNormParam=true` と同等の処理）。
- **LinearWarmupScheduler:** 1エポック目の1バッチ目から正しく学習率が適用されるように、ステップ計算のインクリメントタイミング（オフバイワン）に注意する。

### B. データ分割
- **分割比率:** 80% (Train), 10% (Val), 10% (Test)。
- **分割方法:** ランダムシャッフル (`random_split`) **ではなく**、先頭から順次インデックスを割り当てる（実機の `subList(0, trainCount)` と同じ挙動）。データ自体のシャッフルは、Train用 DataLoader を生成する前に一度だけ全体を `randperm` する。

### C. 勾配爆発と例外処理 (NaN / Inf)
- 実機環境では、通信環境やデータの偏りによってモデルのテンソルが `NaN` や `Inf` になることがあります。
- **改修指示:** シミュレーション内で意図的に（または確率的に）NaNが発生した際、エラーで落ちるのではなく、**NaNを含んだままEdgeに送信**される挙動をシミュレートしてください。

---

## 3. 推論とAP切り替え (Inference & Satisfaction)

実機の `TrainingViewModel.kt` および `start.py` (スナップショット機能) の要となる部分です。

### A. 推論ロジック
- 端末は新しいグローバルモデルを受け取ると、まず自身の現在のネットワーク状況とアプリ種別を元に推論を行います。
- **入力特徴量ベクトル:** `[TP_measured, RTT_measured, AppType_0, AppType_1, AppType_2, AppType_3]` (最初の2つは実測値、以降はアプリのOne-Hotエンコーディング)。
- **出力:** 各AP（例: WiFi, Cellular）に対する Softmax 確率。

### B. 切り替え判定 (`score_diff`)
- 推論された確率の差分（`score_diff = abs(prob[0] - prob[1])`）が、**設定された閾値（例: `delta_threshold = 0.15`）より大きい場合のみ**、新しいAPへ切り替えます。

### C. 満足度 (Satisfaction) 計測フロー
- **改修指示:** シミュレーションの1ラウンドは、以下のフローを忠実に再現する必要があります。
  1. `satisfaction_before` (学習/推論前) の満足度を計算。
  2. 推論を実行し、必要に応じてAPを切り替え。
  3. `satisfaction_after` (切り替え後) の満足度を計算。
  4. ローカル学習を実行。
  5. メトリクスとして `accuracy`, `loss`, `satisfaction_before`, `satisfaction_after` をサーバへ送信。

---

## 4. サーバ側集約における例外排除 (Robust Aggregation)

実機の `Serverside_HFL/edge_server/endpoints/aggregation.py` の `_fedavg_state_dicts_weighted` 関数には、頑健性向上のためのロジックが含まれています。

- **改修指示:** シミュレータの `sim/aggregator.py` にある `_fedavg_weighted` が以下の仕様を満たしているか確認してください。
  - 受信した端末の重み辞書 (`state_dict`) を走査し、`torch.isnan(t).any()` または `torch.isinf(t).any()` が True となるテンソルが含まれている場合、**その端末の更新をまるごと集約から除外（スキップ）**する。
  - 除外された端末の数はログ（警告）として出力し、全体のサンプル数 `total_n` から除外端末のサンプル数を引いて重み付け（Weighted Average）を再計算する。

---

## 5. テレメトリと分析の互換性

シミュレータが吐き出す結果（`results/logs/rounds_*.jsonl`）は、実機のログを分析する `analyze_latest_trial.py` でそのままパースできる必要があります。

- **改修指示:** `Sim_HFL/run_sim.py` またはロガーの実装を修正し、シミュレーションが出力するJSONに実機と同じキーを含めてください。
  - `terminal_id` (または `device_id`)
  - `round`
  - `accuracy`
  - `loss`
  - `app_type` (文字列)
  - `satisfaction_before`
  - `satisfaction_after`
  - `tp_measured_mbps`
  - `rtt_measured_ms`
  - (任意) `event_timestamp`

これにより、`compare_sim_real.py` 等を用いた「実機データ vs シミュレーションデータ」の比較検証が、スクリプトの改修なしで行えるようになります。
