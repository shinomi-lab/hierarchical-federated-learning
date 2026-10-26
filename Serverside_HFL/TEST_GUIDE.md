# HFL テスト実行ガイド

初回セットアップからテスト実行まで、コピペで動くようにまとめています。

---

## STEP 1 — 初回のみ: 仮想環境のセットアップ

```bash
cd ~/Downloads/Serverside_HFL

# venv を作成してパッケージをインストール（5〜10分かかります）
bash setup_venv.sh
```

完了すると以下のような出力が出ます：

```
  uvicorn : 0.xx.x
  fastapi : 0.xxx.x
  torch   : 2.x.x
セットアップ完了！
```

> **2回目以降はこのステップ不要です。**

---

## STEP 2 — 毎回: 仮想環境の有効化

ターミナルを開くたびに実行します。

```bash
cd ~/Downloads/Serverside_HFL
source .venv/bin/activate
```

プロンプトの先頭に `(.venv)` が付いていれば OK です。

---

## STEP 3 — テストを実行する

4 つのモードがあります。上から順に試すのがおすすめです。

---

### モード B — ドライラン（最速・サーバ不要）

FL コアロジック（FedAvg・NaN除外・メトリクスDB）を直接テストします。
**サーバを起動しなくてよいので、まず最初にこれで動作確認しましょう。**

```bash
python test_runner.py B
```

全テスト PASS なら以下のように表示されます：

```
  ✓ FedAvg 正常系  (期待値=2.5000, 実値=2.5000)
  ✓ NaN 含む更新を除外して FedAvg 実行
  ✓ 全 NaN 更新 → ValueError 正常
  ✓ メトリクス DB 書き込み・読み取り
  ✓ エッジ FedAvg (terminal→edge)

  ✓ モード B 全テスト PASS
  ═ PASS ═
```

---

### モード C — E2E スモークテスト（サーバ自動起動・自動停止）

中央サーバ + エッジサーバを自動で起動し、仮想端末で送信、確認後に自動停止します。

```bash
python test_runner.py C
```

オプションで端末数・ラウンド数を変えられます：

```bash
python test_runner.py C --terms 3 --rounds 2
```

---

### モード A — 仮想端末のみ（サーバは自分で起動済みの場合）

別ターミナルで `python start.py` でサーバを起動済みの状態で実行します。

```bash
# 別ターミナルでサーバ起動（localhost を選択）
python start.py --env localhost

# このターミナルで仮想端末を流す
python test_runner.py A --terms 3 --rounds 5
```

---

### モード A+C — フルシミュレーション（複数エッジ・多端末）

複数エッジサーバを自動起動して、より実験に近い条件でテストします。

```bash
# エッジ2台・端末3台/エッジ・3ラウンド
python test_runner.py AC --edges 2 --terms 3 --rounds 3
```

---

## まとめ: よく使うコマンド一覧

| やりたいこと | コマンド |
|---|---|
| 環境有効化 | `source .venv/bin/activate` |
| ドライラン（最速確認） | `python test_runner.py B` |
| E2E スモーク | `python test_runner.py C` |
| 仮想端末のみ送信 | `python test_runner.py A` |
| フルシミュレーション | `python test_runner.py AC --edges 2 --terms 3` |
| サーバ起動（通常実験） | `python start.py` |
| サーバ死活確認 | `python start.py status` |
| AI 実験サマリ生成 | `python start.py report` |

---

## トラブルシューティング

### `No module named uvicorn` が出る

→ 仮想環境が有効化されていません。

```bash
source .venv/bin/activate
```

### `No module named torch` が出る

→ セットアップが不完全です。再実行してください。

```bash
bash setup_venv.sh
```

### モード C/AC でサーバが `タイムアウト` になる

→ ポート 8000・8001 が既に使用中の可能性があります。

```bash
# 使用中のプロセスを確認
lsof -i :8000
lsof -i :8001

# 必要に応じて kill
kill -9 <PID>
```

### モード C/AC でサーバが起動するが集約が進まない

→ 端末数と集約閾値が合っていない可能性があります。
`--terms 1` で試してみてください。

```bash
python test_runner.py C --terms 1 --rounds 2
```

---

## テストモードの使い分けイメージ

```
普段の開発中
  └─ B（ドライラン）: コード変更後にすぐ確認

実験前の動作確認
  └─ C（E2Eスモーク）: サーバ一式が正常に動くか確認

本番に近い検証
  └─ AC（フルシミュ）: 複数エッジ・多端末でのストレステスト

サーバが起動した状態で端末動作だけ確認
  └─ A（仮想端末）: start.py と組み合わせてメトリクス確認
```
