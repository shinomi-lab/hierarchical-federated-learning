# HFL Docker（中央・エッジ・論理クライアント）

`docker-compose.yml` と各 `Dockerfile.*` をこのディレクトリにまとめています。`terminal_client/` のソースはリポジトリルートのままです（ビルド context がルートのため）。

## ビルド・起動

リポジトリルートから:

```bash
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up
```

または `docker/` に移動して:

```bash
cd docker
docker compose build
docker compose up
```

ルートに `compose.yaml` がある場合は、ルートで `docker compose up --build` でも同じ定義を読み込めます。

## ターミナル出力をファイルに残す（セッション記録）

`docker compose up` で画面に流れる**全サービス混在ログ**を、そのまま `docker/logs/compose_YYYYMMDD_HHMMSS.log` に書き込みます（表示もそのまま）。

**書き方**: 長いセッションや異常終了でも欠けにくいよう、**本文は実行中に逐次追記**します。**停止時（正常終了・Ctrl+C 含む）**に `EXIT` トラップで**フッタ（終了時刻）だけを末尾に追記**して締めます。「停止するまでファイルに一切触れない」方式は、メモリ占有とクラッシュ時の全損失のリスクがあるため採用していません。

リポジトリルートから:

```bash
bash docker/run_with_log.sh
```

ビルドを挟まず起動だけ記録する場合:

```bash
bash docker/run_with_log.sh --no-build
```

手動で同等のことだけする場合の例:

```bash
docker compose up --build 2>&1 | tee "docker/logs/compose_manual_$(date +%Y%m%d_%H%M%S).log"
```

## サーバをホスト（Cursor）／論理クライアントだけ Docker

中央・エッジを **PC 上の uvicorn** で動かし、**Docker では論理クライアントだけ**動かす場合。

1. **エッジ起動時に接続先 URL をファイルへ書き出す（任意）**  
   `Serverside_HFL` でエッジを起動するとき、次を付けると `docker/generated-logical-client.env` が生成されます（中身は `EDGE_URL` と同じホスト・ポートを論理クライアント用に並べたもの）。

   ```bash
   export HFL_EXPORT_LOGICAL_CLIENT_ENV=1
   ```

   出力先を変える: `HFL_LOGICAL_CLIENT_ENV_OUT=/path/to/foo.env`  
   2 台目エッジのポート（`terminal-02` 用）を変える: `HFL_LOGICAL_CLIENT_SECOND_EDGE_PORT=8002`

2. **論理クライアントだけ起動**（リポジトリルート）

   ```bash
   docker compose --env-file docker/generated-logical-client.env \
     -f docker/docker-compose.logical-clients.yml up --build
   ```

   自動生成ファイルがまだない場合は、`host.docker.internal` 既定でホストの 8001/8002 に向きます（Docker Desktop on Mac / Windows 向け）。

   ```bash
   docker compose -f docker/docker-compose.logical-clients.yml up --build
   ```

手動で URL だけ決める場合は `docker/env.logical.example` をコピーして編集し、`--env-file` で渡してください。

**同一 compose 内のエッジ＋端末**を使う場合でも、`docker/docker-compose.yml` の `EDGE_URL` は環境変数で上書きできます（既定は従来どおり `http://edge-00:8001` 等）。

```bash
export HFL_EDGE_URL_TERMINAL_00=http://host.docker.internal:8001
docker compose up --build
```

## 構成の概要

- **central / edge-*** … ビルド context は `Serverside_HFL/`
- **terminal-*** … ビルド context はリポジトリルート（`Sim_HFL/sim/` と `terminal_client/` を参照）

詳細は `docker-compose.yml` 先頭コメントを参照してください。

## 仮想端末 1 台のメモリを見る

`docker stats` は **コンテナ単位の cgroup**（ホストから見た RAM 使用量）を出すので、論理クライアント 1 台に絞るのに向いています。

**論理クライアントだけ 1 台起動して、直後の使用量を 1 回表示**（エッジはホストで起動済み想定）:

```bash
cd /Users/tetsuya/HFL
bash docker/measure_terminal_mem.sh
```

**1 秒ごとに更新**（学習中のピークを追う）:

```bash
bash docker/measure_terminal_mem.sh watch
```

**すでに `hfl-terminal-00` が動いているとき**に測るだけ:

```bash
bash docker/measure_terminal_mem.sh stats-only
```

手動なら（コンテナ名は環境に合わせて変更）:

```bash
docker stats --no-stream hfl-terminal-00
```

注意: `docker stats` の **MemUsage** は上限（limit）未設定ならホスト RAM に対する相対表示ではなく、**そのコンテナが使っている RSS 相当**の表示です。GPU は使っていない CPU イメージ想定です。
