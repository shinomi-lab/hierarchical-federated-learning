# Android 端末へのデプロイ Runbook

`HFL_terminals` の APK を実機に入れ、**エッジ接続先**と**端末 ID**を初回に設定して実験に参加させるまでの手順です。**端末ごとに APK を分けない**運用（同一ビルドを複数台に配布）を前提にしています。

## 全体の流れ（ざっくり）

1. **PC 側**: 中央・エッジが起動し、実機から届く IP とポートが決まっている（別 Runbook）。
2. **PC 側**: APK をビルドする。
3. **端末**: USB または adb で APK をインストールする。
4. **端末**: アプリを起動し、**エッジ URL（ホスト・ポート）** → **端末 ID（と任意で実験群）** を保存する。
5. **端末**: Wi‑Fi が実験用 SSID（`HFL_A24` / `HFL_B`）のいずれかに接続できる状態にする。

サーバの起動の説明は **`docs/runbook_hfl_server_launch/README.md`** にあります。下の **「コマンド一覧」** に、端末デプロイとセットで使う起動例も載せています。

---

## 前提チェックリスト

| 項目 | 内容 |
|------|------|
| JDK | AGP が要求する JDK。ターミナルで `./gradlew` するなら下記 **「Android Studio の JBR を JAVA_HOME に」** が手軽 |
| Android SDK | Android Studio または CLI の `sdkmanager` で API レベルが揃っていること |
| 実機 OS | アプリの **`minSdk` は 26（Android 8.0）**。それ未満の端末にはインストール不可 |
| `adb` | `adb devices` で対象端末が **device** として見えること |
| ネットワーク | 端末とエッジ PC が **同一 LAN**（またはエッジに届く経路）であること |
| サーバ | 端末が向ける **エッジ**がすでに listen していること |

---

## コマンド一覧（コピー用）

`/path/to/HFL` はこのリポジトリの実パスに置き換えてください（例: `/Users/you/HFL`）。  
**`HFL_HOST`** はエッジを動かす PCの **LAN IPv4**（端末のエッジ設定と同じ値。例: `192.168.11.6`）。

### A. 中央＋エッジ 2 台を 1 ターミナルで起動（推奨）

研究室 LAN プロファイル `shinomilab`、エッジは **8001 / 8002**。

```bash
cd /path/to/HFL/Serverside_HFL
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export HFL_RUN_MODE=production
python3 start.py --run-mode production --env shinomilab --edges 2
```

停止: そのターミナルで **Ctrl+C**。

### B. 中央とエッジをターミナル 3 つに分ける（ログ分離用）

各ターミナルで先に venv を有効化します。

```bash
cd /path/to/HFL/Serverside_HFL
source .venv/bin/activate
```

**ターミナル 1（中央・8000）**

```bash
cd /path/to/HFL/Serverside_HFL
source .venv/bin/activate
export HFL_RUN_MODE=debug
export CENTRAL_AGGREGATION_THRESHOLD=2
python3 -m uvicorn central_server.main:app --host 0.0.0.0 --port 8000
```

**ターミナル 2（エッジ 1 台目・端末 0/1 向け・8001）**

`HFL_HOST` を実 IPに変えてから実行してください。

```bash
cd /path/to/HFL/Serverside_HFL
source .venv/bin/activate
export HFL_HOST=192.168.11.6
export HFL_RUN_MODE=debug
export CENTRAL_SERVER_URL=http://127.0.0.1:8000
export EDGE_SERVER_ID=edge-server-01
export EDGE_URL=http://${HFL_HOST}:8001
export EDGE_AGGREGATION_THRESHOLD=2
export EDGE_AGGREGATION_THRESHOLD_OVERRIDE=2
python3 -m uvicorn edge_server.main:app --host 0.0.0.0 --port 8001
```

**ターミナル 3（エッジ 2 台目・端末 2 向け・8002）**

```bash
cd /path/to/HFL/Serverside_HFL
source .venv/bin/activate
export HFL_HOST=192.168.11.6
export HFL_RUN_MODE=debug
export CENTRAL_SERVER_URL=http://127.0.0.1:8000
export EDGE_SERVER_ID=edge-server-02
export EDGE_URL=http://${HFL_HOST}:8002
export EDGE_AGGREGATION_THRESHOLD=1
export EDGE_AGGREGATION_THRESHOLD_OVERRIDE=1
python3 -m uvicorn edge_server.main:app --host 0.0.0.0 --port 8002
```

### C. 死活確認（任意）

```bash
curl -sS http://127.0.0.1:8000/healthz
curl -sS http://127.0.0.1:8001/healthz
curl -sS http://127.0.0.1:8002/healthz
```

### D. 端末に入れるまでのコマンド（ひとまとめ）

`/path/to/HFL` をこのリポジトリの実パスに置き換えてください。

**`JAVA_HOME`（macOS・ターミナルから `gradlew` するとき）**  
`Unable to locate a Java Runtime` が出る場合は、**Android Studio に同梱の JBR** を指定するのが手軽です。Studio を JetBrains Toolbox 等で別パスに入れているときは `Android Studio.app` の位置に合わせる。`jbr` が無い構成では `Contents/jre/Contents/Home` のことがある。

**1 台だけ USB 接続しているとき** — 次を上から順に（1 つのコピペブロック）:

```bash
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
cd /path/to/HFL/HFL_terminals
./gradlew :app:assembleDebug
adb devices
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

**複数台 USB のとき** — 先に `adb devices` でシリアルを確認し、`<SERIAL>` を左列の値に置き換える:

```bash
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
cd /path/to/HFL/HFL_terminals
./gradlew :app:assembleDebug
adb devices
adb -s <SERIAL> install -r app/build/outputs/apk/debug/app-debug.apk
```

**アンインストール（設定を消して入れ直したいとき）** — 現在の `applicationId` は `com.example.hfl_experiment`:

```bash
adb -s <SERIAL> uninstall com.example.hfl_experiment
```

1 台だけなら `uninstall` / `install` の `-s <SERIAL>` は省略してよい。

---

## 1. APK のビルド（PC）

リポジトリルートから `HFL_terminals` に移動し、Debug APK を生成します。

ターミナルから **ビルドして端末に入れるまで** のコピペ用は、**「コマンド一覧」→ D** のひとまとめブロックを使う。

Android Studio だけでビルドする場合は、メニューから **Build → Build Bundle(s) / APK(s) → Build APK(s)** でもよい（その場合はターミナルの `JAVA_HOME` は不要。インストールは別途 `adb install`）。

成果物の典型パス:

- `HFL_terminals/app/build/outputs/apk/debug/app-debug.apk`

Release が必要な場合はプロジェクトの signing 設定に従い `assembleRelease` を使います。

---

## 2. 端末へのインストール

**コピー用のコマンドは上の「コマンド一覧 → D. 端末に入れるまでのコマンド（ひとまとめ）」に集約**しています。ここでは意味だけ補足します。

- `adb devices` … 端末が **device** と出ていること（`unauthorized` なら端末側で USB デバッグ許可）。
- `adb install -r` … **上書き**インストール。データは原則保持。挙動に迷ったら D の `uninstall` のあとにもう一度 `install`。
- 複数台 … **`-s <SERIAL>`** で対象を指定。

---

## 3. 初回起動でアプリ内設定（必須）

同一 APK でも、**各端末で一度**（またはデータ削除後に再度）次を通します。

### 3.1 エッジ接続先

- **Host**: エッジを動かしている PC の **LAN IPv4**（例: `192.168.11.6`）。`127.0.0.1` は端末からは使えません。
- **Port**: その端末が接続すべき **エッジのポート**。

本プロジェクトの **3 端末・2 エッジ**の想定割当（`start.py` の既定ポートと一致）:

| 端末 | 向けるエッジ | ポートの例 |
|------|----------------|-------------|
| 端末 0, 1 | エッジ 0（1 台目） | **8001** |
| 端末 2 | エッジ 1（2 台目） | **8002** |

アプリのエッジ設定画面では **8001 / 8002 / 8003** のクイック選択がある場合があります。実際に `uvicorn` が listen している番号と一致させてください。

### 3.2 端末 ID / 実験群

- **端末 ID**: サーバ・ログで一意になる文字列（例: `device-001` … 他端末と**重複しない**こと）。
- **実験群**: 運用で必要なら入力（空でも可ならそのまま）。

保存後、メイン画面に進めば設定完了です。

---

## 4. あとから変更する場合（設定アプリ内）

アプリの **設定** から次にアクセスできます。

- **エッジ接続先のみを確認・変更** … ホスト・ポートだけ。
- **端末 ID / 実験群を変更** … 初回と同様の識別子。

ルータ SSID・パス等は設定画面のネットワーク欄にあります（詳細は下記「Wi‑Fi 設定」）。

---

## 5. Wi‑Fi 設定（アセットと既定）

実験用の既定 SSIDは次のとおりです（ルータ側の SSID と一致させる）。

| 役割 | SSID | パスワード |
|------|------|------------|
| 主 AP（例: Wi‑Fi 側） | `HFL_A24` | `Tetsu060986239` |
| 副 AP（例: オープン） | `HFL_B` | なし（オープン） |

アプリ内の参照元は主に次です。

- `HFL_terminals/app/src/main/assets/device_config.json` … `preferred_ssid` / `upload_ssid`（起動時に prefs へ反映）
- `HFL_terminals/app/src/main/assets/ap_config.json` … AP 選択ロジック用の `apA` / `apB` の `ssid` / `psk`
- `AppConfig.kt` … 上記が未設定のときのフォールバック定数

**注意**: 一度インストール済みの端末では、以前の SSID が **SharedPreferences に残っている**と、アセットよりそちらが優先されることがあります。SSID を変えた直後に挙動がおかしい場合は、アプリデータの削除または再インストールを検討してください。

---

## 6. よくあるつまずき

| 現象 | 確認すること |
|------|----------------|
| エッジに繋がらない | PC の IP が変わっていないか。ファイアウォールで **8001/8002** が LAN から通るか。端末のポートが **本当にそのエッジ**か。 |
| 端末 ID が意図と違う | 設定の「端末 ID / 実験群を変更」、またはデータ削除後に再設定。 |
| Wi‑Fi 切替がおかしい | `device_config.json` / `ap_config.json` / prefs の SSID がルータと一致しているか。オープン AP はコード側で PSK 未設定扱いになるよう実装済み。 |
| `adb` が見えない | USB デバッグ有効、ケーブル、ドライバ（Windows）。`adb kill-server` → `adb start-server`。 |

---

## 7. 関連ドキュメント

| パス | 内容 |
|------|------|
| `docs/runbook_hfl_server_launch/README.md` | 中央・エッジの起動（`start.py` / 手動 `uvicorn`） |
| `Serverside_HFL/RUN_GUIDE.md` | 環境変数・複数エッジの詳細 |
| `.cursor/rules/hfl-architecture.mdc` | 3 層 HFL の端末・エッジ対応関係 |

---

## 8. エラーレポート（学習画面）とエッジ上の保存場所

### 端末アプリ

学習タブで **「学習を中止」** のあと（またはそのまま）**「エラーレポート送信（ログをエッジへ）」** を押すと、スナップショットがエッジの `upload_client_logs` に送られます。

試行をまたぐときに **OS のアプリキャッシュ削除は基本不要**です。学習画面の下の方にある次のボタンで足ります。

- **保存ラウンドを消す** … 端末が保持している「最後に認識したラウンド」を消し、次のメタを新しいラウンドとして扱う  
- **学習完了をやり直す** … 「学習完了」表示を消し、**Start 5 Cycles** を再度押せるようにする  

### エッジサーバ（ディスク）

リポジトリからの相対パス（既定 `TERMINAL_LOGS_DIR`）:

| 種類 | パス |
|------|------|
| エラーレポート（1 回の送信 = 1 フォルダ） | `Serverside_HFL/received_files/terminal_logs/error_reports/<terminal_id>/<YYYYMMDD_HHMMSS_******>/` |
| そのフォルダの中身 | 元ファイル名に近い名前のログ、`README.txt`（説明）、`request_meta.json`（端末が付けた meta） |
| 一覧の追跡用 | 同じく `error_reports/incoming_batches.jsonl` に 1 行ずつ追記 |
| 従来の通常アップロード | `received_files/terminal_logs/<terminal_id>/r<ラウンド>/<YYYYMMDD>/` 配下（SHA ファイル名） |

---

## 9. この文書の保守

手順や既定 SSID、ポート割当、ログ保存レイアウトを変えたら **本 README を正**として更新してください。サーバ側だけの変更は `runbook_hfl_server_launch` 側に書き分けると重複が減ります。
