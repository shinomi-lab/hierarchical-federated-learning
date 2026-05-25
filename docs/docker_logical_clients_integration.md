# Docker 論理クライアント統合設計書

**作成日**: 2026-05-14
**目的**: 実端末 (Android) + Docker 仮想端末の混在実験を実現する

---

## 1. 目標構成

```
┌─────────────────────────────────────────────────────────┐
│ Mac (Cursor ターミナルで起動)                              │
│                                                         │
│   中央サーバ (port 8000)                                  │
│   エッジサーバ-00 (port 8001)                              │
│   エッジサーバ-01 (port 8002)                              │
│                                                         │
│   ┌─────────────────────────────┐                       │
│   │ Docker                       │                       │
│   │  terminal-00 ──→ edge-00     │                       │
│   │  terminal-01 ──→ edge-00     │                       │
│   │  terminal-02 ──→ edge-01     │                       │
│   │  ...          ──→ ...        │                       │
│   │  terminal-N   ──→ edge-XX    │                       │
│   └─────────────────────────────┘                       │
│         host.docker.internal:8001/8002                   │
└────────────────────┬────────────────────────────────────┘
                     │ Wi-Fi LAN
        ┌────────────┼────────────┐
        │            │            │
   Android-01   Android-02   Android-03   Android-04
   → edge-00    → edge-00    → edge-01    → edge-01
```

**起動時に Docker 端末台数を指定可能にする**:
```bash
# 例: Docker端末 8台 + 実端末 4台 = 合計12台
./run_logical_clients.sh --terminals 8 --edge-00-url http://host.docker.internal:8001 --edge-01-url http://host.docker.internal:8002
```

---

## 2. 現状の課題と対応方針

### 課題 1: 集約閾値の動的設定 [重要度: 高]

**問題**: エッジサーバの `EDGE_AGGREGATION_THRESHOLD` は「何台分の重みが届いたら FedAvg 集約を実行するか」を指定する。実端末 + Docker 端末の合計に合わせないと、集約が途中で発火するか永遠に待ち続ける。

**現状**:
- edge-00: `EDGE_AGGREGATION_THRESHOLD=2`
- edge-01: `EDGE_AGGREGATION_THRESHOLD=1`

**対応方針**: エッジサーバ起動時に実端末数 + Docker端末数の合計を閾値として設定する。

```bash
# Cursor ターミナルでの起動例
EDGE_AGGREGATION_THRESHOLD=5 python -m edge_server.main  # edge-00: 実2台 + Docker3台
EDGE_AGGREGATION_THRESHOLD=3 python -m edge_server.main  # edge-01: 実2台 + Docker1台
```

**追加検討**: 実端末は遅延・切断があり得るため、タイムアウト付き集約（「N台 or T秒経過で集約」）の導入も将来的に検討。ただし現状は同期式で十分。

---

### 課題 2: API エンドポイントの統一 [重要度: 高]

**問題**: Docker 端末 (`terminal_client/main.py`) は `/upload_biases` で重みを送信するが、実端末 (Android) は `/receive_terminal_weights/{terminal_id}` を使用する。

**現状のエンドポイント**:

| エンドポイント | 送信元 | 用途 |
|---------------|--------|------|
| `POST /upload_biases` | Docker 端末 | 重みアップロード (旧) |
| `POST /receive_terminal_weights/{terminal_id}` | Android 実端末 | 重みアップロード (現行) |

**対応方針**: `terminal_client/main.py` の `_upload_weights()` を `/receive_terminal_weights/{terminal_id}` に変更する。実端末と同じエンドポイント・同じペイロード形式を使うことで、エッジサーバ側での集約処理が統一される。

**変更箇所**: `terminal_client/main.py` L144-158

```python
# 変更前
def _upload_weights(client, model):
    floats = _model_to_floats(model)
    payload = {"biases": floats, "terminal_id": TERMINAL_ID}
    r = client.post(f"{EDGE_URL}/upload_biases", json=payload, timeout=30.0)

# 変更後: 実端末と同じエンドポイント・形式を使用
def _upload_weights(client, model):
    weight_bin = model.to_f32_flat()
    # multipart/form-data で .bin ファイルとして送信（実端末と同一）
    files = {"file": (f"{TERMINAL_ID}_weights.bin", weight_bin, "application/octet-stream")}
    data  = {"terminal_id": TERMINAL_ID, "round_id": str(current_round)}
    r = client.post(
        f"{EDGE_URL}/receive_terminal_weights/{TERMINAL_ID}",
        files=files, data=data, timeout=30.0
    )
```

**確認事項**: `/receive_terminal_weights/{terminal_id}` のペイロード形式（multipart? JSON?）を `edge_server/endpoints/terminal_update.py` で確認し、Docker 端末を正確に合わせる必要がある。

---

### 課題 3: ラウンド同期 [重要度: 高]

**問題**: Docker 端末は `for g_rnd in range(NUM_ROUNDS)` で自分のペースで進む。実端末はエッジサーバの `/api/v1/meta` をポーリングしてラウンド番号が上がるのを待つ。Docker 端末が先走ると集約タイミングがずれる。

**現状のフロー比較**:

| 処理 | 実端末 (Android) | Docker端末 (現状) |
|------|-----------------|------------------|
| ラウンド開始 | `/api/v1/meta` をポーリング、round が変わったら開始 | 自分で `for` ループ |
| モデル取得 | `/send_to_device` → `/download` | 同じ |
| 学習 | LocalTrainer.kt | SimTerminal.local_train() |
| 重み送信 | `/receive_terminal_weights/{id}` | `/upload_biases` |
| 次ラウンド待ち | `/api/v1/meta` ポーリング | `time.sleep(2)` |

**対応方針**: Docker 端末にも `/api/v1/meta` ポーリングを追加する。

```python
# terminal_client/main.py に追加
def _wait_for_round(client, expected_round, timeout=300):
    """エッジサーバのラウンドが expected_round になるまでポーリング"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = client.get(f"{EDGE_URL}/api/v1/meta", timeout=5.0)
            if r.status_code == 200:
                meta = r.json()
                server_round = meta.get("round", 0)
                if server_round >= expected_round:
                    return server_round
        except Exception:
            pass
        time.sleep(3.0)
    raise TimeoutError(f"Round {expected_round} not reached in {timeout}s")
```

**メインループの変更**:
```python
for g_rnd in range(NUM_ROUNDS):
    # ラウンド同期: エッジサーバが次ラウンドに進むまで待機
    _wait_for_round(client, g_rnd + 1)

    # モデルダウンロード → 学習 → アップロード
    ...
```

---

### 課題 4: モデルサイズの可変化 [重要度: 中]

**問題**: `terminal_client/main.py` L222-224 で `input_size=6, output_size=4` がハードコードされている。15次元モデルや 3AP (output_size=3) に対応できない。

**対応方針**: 環境変数で設定可能にする。

```python
INPUT_SIZE   = int(os.environ.get("INPUT_SIZE",   "6"))
HIDDEN_SIZE  = int(os.environ.get("HIDDEN_SIZE",  "32"))
OUTPUT_SIZE  = int(os.environ.get("OUTPUT_SIZE",   "4"))
DROPOUT_P    = float(os.environ.get("DROPOUT_P",  "0.1"))
```

**または**: ダウンロードしたモデルのパラメータ数から自動推定する。ただし明示的な環境変数の方が安全。

---

### 課題 5: 教師データの共有 [重要度: 中]

**問題**: Docker 端末は `APSelectionDataset(n_samples=NUM_SAMPLES, seed=SEED)` で合成データを自前生成する。実端末は `assets/training_data.csv` を使う。同じ教師データでないと、Docker 端末と実端末で学習の前提が異なる。

**対応方針 (3案)**:

| 案 | 方法 | メリット | デメリット |
|----|------|---------|-----------|
| A | CSV をコンテナにマウント | 確実に同一データ | docker-compose にボリューム追加が必要 |
| B | エッジサーバから CSV をダウンロード | 動的対応可 | エッジに配信エンドポイント追加が必要 |
| C | seed 固定で合成データを使う | 変更不要 | 実端末と厳密には異なる |

**推奨: 案 A**（CSV マウント）。docker-compose で:
```yaml
terminal-00:
  volumes:
    - ../training_data_list/default/training_data.csv:/app/data/training_data.csv:ro
  environment:
    CSV_PATH: "/app/data/training_data.csv"
```

---

### 課題 6: 推論の扱い [重要度: 低]

**問題**: 実機実験では推論はエッジサーバ上で `start.py` のタブキー押下で実行される。Docker 端末は自前で `run_inference()` を呼んでいる。

**対応方針**: 学習・重みアップロードのみ Docker 端末が担当し、推論は従来通り `start.py` で一括実行する。Docker 端末の推論結果はログ用とし、AP 切り替え判断には使わない。

---

## 3. 端末台数の動的設定

### 3.1 compose 生成スクリプト

`docker/gen_compose.py` を作成し、端末台数・エッジ割当を起動時に指定可能にする。

```bash
# 使い方
python docker/gen_compose.py \
  --terminals 8 \
  --edge-00-url http://host.docker.internal:8001 \
  --edge-01-url http://host.docker.internal:8002 \
  --edge-00-count 5 \
  --edge-01-count 3 \
  --output docker/docker-compose.generated.yml

# 起動
docker compose -f docker/docker-compose.generated.yml up --build
```

**生成される compose の例 (8台)**:
```yaml
services:
  terminal-00:
    environment:
      TERMINAL_ID: "terminal-00"
      EDGE_URL: "http://host.docker.internal:8001"
      APP_CATEGORY: "browser"
      SEED: "100"
  terminal-01:
    environment:
      TERMINAL_ID: "terminal-01"
      EDGE_URL: "http://host.docker.internal:8001"
      APP_CATEGORY: "video"
      SEED: "101"
  ...
  terminal-07:
    environment:
      TERMINAL_ID: "terminal-07"
      EDGE_URL: "http://host.docker.internal:8002"
      APP_CATEGORY: "other"
      SEED: "107"
```

### 3.2 ラッパースクリプト

`run_logical_clients.sh` — compose 生成 + ビルド + 起動を一括実行:

```bash
#!/bin/bash
# 使い方: ./run_logical_clients.sh --terminals 8
#
# オプション:
#   --terminals N       Docker端末台数 (default: 4)
#   --edge-00-count N   edge-00 に割り当てる台数 (default: 自動振り分け)
#   --edge-00-url URL   edge-00 の URL (default: http://host.docker.internal:8001)
#   --edge-01-url URL   edge-01 の URL (default: http://host.docker.internal:8002)
#   --seed-offset N     シード開始値 (default: 100, 実端末と重ならないように)

python3 docker/gen_compose.py "$@" --output docker/docker-compose.generated.yml
docker compose -f docker/docker-compose.generated.yml up --build
```

### 3.3 エッジサーバ閾値の自動計算

起動スクリプトにエッジサーバの閾値計算を組み込む:

```bash
# 例: 実端末2台 + Docker5台 = edge-00 の閾値は7
REAL_TERMINALS_EDGE00=2
DOCKER_TERMINALS_EDGE00=5
THRESHOLD_EDGE00=$((REAL_TERMINALS_EDGE00 + DOCKER_TERMINALS_EDGE00))

EDGE_AGGREGATION_THRESHOLD=$THRESHOLD_EDGE00 python -m edge_server.main
```

---

## 4. 実装順序

```
Phase 1: 最低限動くようにする
├── 1-1. terminal_client/main.py の API エンドポイント統一 (課題2)
├── 1-2. terminal_client/main.py にラウンド同期ポーリング追加 (課題3)
└── 1-3. モデルサイズの環境変数化 (課題4)

Phase 2: 端末台数の動的設定
├── 2-1. docker/gen_compose.py 作成
├── 2-2. run_logical_clients.sh 作成
└── 2-3. エッジサーバ閾値の自動計算

Phase 3: 混在実験の実行
├── 3-1. 実端末4台 + Docker4台 (計8台) で動作確認
├── 3-2. 教師データの統一 (課題5)
└── 3-3. 実端末4台 + Docker8〜12台 (計12〜16台) でスケール検証
```

---

## 5. 混在実験の運用フロー

```
1. Cursor ターミナルで中央サーバ・エッジサーバを起動
   $ cd Serverside_HFL
   $ EDGE_AGGREGATION_THRESHOLD=7 python -m edge_server.main  # edge-00 (実2 + Docker5)
   $ EDGE_AGGREGATION_THRESHOLD=5 python -m edge_server.main  # edge-01 (実2 + Docker3)
   $ python -m central_server.main                             # central

2. Docker 仮想端末を起動
   $ ./run_logical_clients.sh --terminals 8 --edge-00-count 5 --edge-01-count 3

3. Android 実端末を起動 (4台)
   - 端末1,2 → edge-00 (192.168.11.2:8001)
   - 端末3,4 → edge-01 (192.168.11.2:8002)

4. 学習が進行（同期式: 全端末の重みが届いたら集約）

5. 推論はエッジサーバ上で start.py のタブキーで実行
   （Docker端末 + 実端末の全メトリクスを使って一括推論）
```

---

## 6. リスクと対策

| リスク | 影響 | 対策 |
|--------|------|------|
| Docker端末が実端末より早く完了し、集約が部分的に発火 | 集約品質の低下 | ラウンド同期ポーリング (課題3) で解決 |
| 実端末が切断/遅延し、集約閾値に達しない | 全体がブロック | タイムアウト付き集約 or 閾値を `実端末数のみ` に設定しDocker端末は「追加参加」扱い |
| Docker端末と実端末で教師データが異なる | 学習の前提不一致 | 同一CSVをマウント (課題5) |
| Mac のリソース不足 (Docker端末多すぎ) | OOM / 遅延 | 1端末あたり ~300MB。8台で ~2.4GB。16台で ~4.8GB。Mac のメモリに応じて調整 |
| terminal_id の重複 | 集約の混乱 | Docker端末は `terminal-00`〜、実端末は `sim-term-XX` や `pixel-XX` など命名規則を分ける |

---

## 7. まとめ

**解決すべき課題は6つあるが、Phase 1 の 3つ（API統一・ラウンド同期・モデルサイズ可変化）を直せばとりあえず動く。** 端末台数の動的設定は `gen_compose.py` で対応可能。

基盤（Dockerfile, terminal_client, API仕様）は既に存在するため、ゼロからの構築ではなく改修レベルの作業量。
