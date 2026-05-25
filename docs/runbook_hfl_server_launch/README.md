# HFL サーバ起動ランブック

このフォルダは **中央・エッジの実行方法**（`start.py` の **本番実験ラン / デバッグラン**、および **別ターミナルでの個別起動**）を一箇所に固定するための **専用ランブック**です。詳細な環境変数一覧は `Serverside_HFL/RUN_GUIDE.md` を参照してください。

**ログを中央とエッジで分けて見たい**場合は、**`start.py` ではなく、ターミナルを 2 つ開いて uvicorn を個別起動**する方針が向いています（下記「別ターミナルで個別起動」）。

---

## 前提

- 作業ディレクトリ: **`Serverside_HFL/`**
- **同じ venv** を両ターミナルで有効化する（例: `source .venv/bin/activate`）。未設定の **`CommandLineTools` の `python3` だけ**では `uvicorn` が無いことがあります。
- 初回のみ: `pip install -r requirements.txt`
- **`start.py` を使う場合**は、その `python` と同じインタプリタで子の uvicorn も起動されます（venv 上で `python3 start.py`）。

### `No module named uvicorn` が出るとき

macOS の **`/Library/Developer/CommandLineTools/usr/bin/python3`** など、依存が入っていないインタプリタで起動していることが多いです。**プロジェクト用 venv** を使ってください。

```bash
cd Serverside_HFL
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 start.py
```

（Windows の場合は `.venv\Scripts\activate`）

---

## 別ターミナルで個別起動（ログを分けたいとき）

**起動順**: 先に **中央**、次に **エッジ**（エッジは起動時に中央へ問い合わせる処理があるため）。

両方のターミナルで毎回（**この順を飛ばすと** `No module named uvicorn` になります。`python3` が **`/Library/Developer/CommandLineTools/...`** になっているのは venv 未使用です）:

```bash
cd /path/to/Serverside_HFL
source .venv/bin/activate
which python3    # .../Serverside_HFL/.venv/bin/python3 になっていることを確認
```

venv を有効にしたくない場合は、**フルパス**でも可です（例）:

```bash
cd /path/to/Serverside_HFL
.venv/bin/python3 -m uvicorn ...
```

### ターミナル 1: 中央サーバ

```bash
export HFL_RUN_MODE=debug   # または production。未設定でも動作します（ログ用ラベル）
export CENTRAL_AGGREGATION_THRESHOLD=1   # エッジ1台なら 1 推奨
python3 -m uvicorn central_server.main:app --host 0.0.0.0 --port 8000
```

ログはこのウィンドウにだけ流れます。

### ターミナル 2: エッジサーバ（1 台・ポート 8001）

**同一 PC だけ**（エミュレータや curl、端末も USB 経由でホストに届く等）で試す例:

```bash
export HFL_RUN_MODE=debug
export CENTRAL_SERVER_URL=http://127.0.0.1:8000
export EDGE_URL=http://127.0.0.1:8001
export EDGE_SERVER_ID=edge-server-01
export EDGE_AGGREGATION_THRESHOLD=1
export EDGE_AGGREGATION_THRESHOLD_OVERRIDE=1
python3 -m uvicorn edge_server.main:app --host 0.0.0.0 --port 8001
```

**実機 Android が同一 Wi‑Fi から PC に接続する**場合は、`EDGE_URL` を **端末の設定と同じ**にします（例: `http://192.168.11.6:8001`）。`CENTRAL_SERVER_URL` は **エッジプロセスから見た中央の URL** なので、中央がその PC の 8000 で動いていれば通常は `http://127.0.0.1:8000` のままでよいです（中央を別マシンに置く構成なら、そのホストに変更）。

停止は **各ターミナルで Ctrl+C**（先にエッジを止め、次に中央、でもよいが通信エラーはエッジ側に出る程度）。

### 研究室 LAN の IP を手で合わせる（`start.py` なし）

`start.py` の **shinomilab 動的 IP** は使わないため、**この PC の IPv4**（`ifconfig` / システム設定で確認）を `EDGE_URL` と端末アプリのベース URL に揃えます。

---

## 構造（何が起きているか）

`start.py` は **ランチャー**です。起動するのは常に次の **同一実装**の uvicorn です（**ログは色分けで 1 画面に混在**します）。

| 役割 | モジュール | 既定ポート（`start.py` 利用時） |
|------|------------|-----------------------------------|
| 中央サーバ | `central_server.main:app` | 8000 |
| エッジサーバ | `edge_server.main:app` | 8001 から連番（2台目は 8002） |

**本番実験ラン（`production`）** と **デバッグラン（`debug`）** は、別バイナリではなく、ランチャーが決める **エッジ台数・集約閾値の既定**と、子プロセスに渡す **`HFL_RUN_MODE`** が変わります。ルーティングや FedAvg の本体コードはモードで切り替わりません（明示オプションで上書き可能）。

### `shinomilab`（研究室 LAN）だけ動的 IP

`start.py` で **`shinomilab`** を選んだとき（`--env shinomilab` 含む）、`CENTRAL_SERVER_URL` / 各 `EDGE_URL` に使う **ホスト IP は固定値ではなく、このマシンを推定**します。

1. このホストに **`192.168.11.0/24` の IPv4** があれば優先（`en0` などよくある IF 名を先に見る）  
2. 無い場合は **外向き UDP（8.8.8.8）のローカル終端**でデフォルト経路側のアドレスを使う  
3. それも無い場合は従来の既定 **`192.168.11.2`** にフォールバック  

起動時に 1 行、採用した IP と理由が表示されます。**手動で `uvicorn` だけ起動する場合**は `start.py` を経由しないため、この自動検出は効きません（`CENTRAL_SERVER_URL` / `EDGE_URL` を手で合わせる必要があります）。

---

## 本番実験ラン（`production`）

実機実験・採用データに近い **従来の既定**で動かすモードです。

### 対話で起動

```bash
cd Serverside_HFL
python3 start.py
```

1. **ラン種別**で **P**（本番実験ラン）
2. **環境プロファイル**（研究室 LAN 等）
3. **エッジ台数**（例: 2）
4. **AI アドバイザー**の有無

### 非対話の例（エッジ2台・研究室プロファイル）

```bash
python3 start.py --run-mode production --env shinomilab --edges 2
```

### 既定の挙動（オプション未指定時）

- **エッジ台数**: `--edges` を付けない対話では「エッジサーバの台数」を聞く（既定文字列は `1`）。
- **端末集約閾値**（`--edge-thresholds` 省略時）: エッジ **2 台**なら **`[2, 1]`**（2+1 端末想定）。それ以外の台数は各エッジ **1**。
- **中央集約閾値**（`--central-threshold` および `CENTRAL_AGGREGATION_THRESHOLD` 未設定時）: **エッジ台数と同じ**（全エッジ分そろってからグローバル集約）。

停止: **Ctrl+C**（中央・全エッジを一括停止）。

---

## デバッグラン（`debug`）

手元での単発確認・修正検証向け。**最小トポロジ寄りの既定**です。

### 非対話（推奨・迷いが少ない）

```bash
cd Serverside_HFL
python3 start.py --run-mode debug --env localhost --edges 1
```

`--edges` を省略した場合は **エッジ台数は 1 に固定**（質問しない）。複数エッジで試すときだけ `--edges N`。

### 対話で起動

```bash
python3 start.py
```

ラン種別で **D**（デバッグラン）。

### 既定の挙動（オプション未指定時）

- **エッジ台数**: `--edges` 省略時は **1**（固定）。複数は `--edges N`。
- **端末集約閾値**（`--edge-thresholds` 省略時）: **常に全エッジで 1**（`[1, …, 1]`）。2 台でも **`[2,1]` にはしない**。
- **中央集約閾値**: 未指定時はエッジ台数に合わせるため、エッジ 1 台なら **1**。

---

## ラン種別の決め方まとめ

| 項目 | `production` | `debug` |
|------|----------------|---------|
| CLI | `--run-mode production` | `--run-mode debug` |
| 対話 | **P** | **D** |
| `--edges` 省略時 | 台数を質問 | **1 固定** |
| `--edge-thresholds` 省略時 | 2 エッジなら `2,1`、他は各 1 | **常に全 1** |
| 子プロセスの環境 | `HFL_RUN_MODE=production` | `HFL_RUN_MODE=debug` |

**両モード共通**: `--edges` / `--edge-thresholds` / `--central-threshold` を付ければ **既定より優先**されます。

---

## 死活確認

```bash
cd Serverside_HFL
python3 start.py status
```

または `GET http://127.0.0.1:8000/healthz`（中央）、`GET http://127.0.0.1:8001/healthz`（エッジ1）など。

---

## 関連ドキュメント

| パス | 内容 |
|------|------|
| `docs/runbook_android_terminal_deploy/README.md` | **実機 APK のビルド・インストール・初回設定（エッジ URL / 端末 ID）** |
| `Serverside_HFL/RUN_GUIDE.md` | 環境変数リファレンス、複数エッジ、トラブルシューティング |
| `Serverside_HFL/start.py` | ランチャー実装（`--run-mode`、閾値計算） |

---

## このフォルダについて

意図: **実行手順が `RUN_GUIDE.md` や会話ログに埋もれないよう**、起動ランブックだけを **`docs/runbook_hfl_server_launch/`** に分離しました。手順の変更があれば **本 README を正**として更新してください。
