# Implementation Notes — 5-round automation design and changes

このファイルは、5ラウンド分の自動実行を目指すために必要な変更点・現在行った変更・今後の作業をまとめたドキュメントです。

## 1) このセッションで実際に加えた変更
- `edge_server/utils/__init__.py` を追加
  - `get_latest_batch_dir`, `save_uploaded_file` などユーティリティを外部公開するためのエクスポートを追加しました。
  - 目的: `edge_server.endpoints.model_ops` などから `edge_server.utils` をインポートしたときの ImportError を解消するため。

> 注: コードの大幅な振る舞い変更は未実施（安全側の小修正のみ）。

## 2) すぐに実施すべき修正（短期優先）
1. `edge_server/utils` に存在するユーティリティ関数を明確化・テストする
   - `file_utils.py` の `get_latest_batch_dir` は文字列引数を期待しているが、呼び出し元が Path を渡す箇所がある。
   - 互換のため `Path | str` を受け付けるように調整すると安全。

2. `run_all` 相当の起動スクリプトを整備して起動順（中央→エッジ）を保証する
   - `scripts/run_simulation.py`（このリポジトリに追加済み）で簡易的に起動/待機する。

3. 集約と送信の初歩的な単体テストを追加
   - `edge_server/endpoints/aggregation.py` の `_fedavg_state_dicts_weighted` は既に詳細に実装されているが、ユニットテストを追加してください。

## 3) 中長期の実装計画（要約）
- 中央サーバ
  - `/start_round`, `/round_status` の追加
  - `central_server/state.py` に `round_meta` を組み込み、受信済みエッジ追跡・タイムアウト処理を実装
  - 受信ファイルの永続化（`received_edges/<round>/<edge_id>/`）と `global_models/round_<n>.pt` の履歴保存

- エッジサーバ
  - `/start_training_for_round` エンドポイントを実装（中央からラウンド開始通知）
  - `aggregate_and_send_to_central_server` のリファイン（既存の実装はあるが、robust な送信/再試行/失敗時永続化を強化）
  - ステートマシン実装（IDLE→COLLECT→AGGREGATE→SEND 等）

- 端末（Android Kotlin）
  - `/api/v1/meta` と `/receive_terminal_weights/{terminal_id}` のスキーマ確定
  - OkHttp での multipart upload 実装、再試行ルール

## 4) この環境では実行できない点（重要）
- 私はこのワークスペース内で外部プロセス（uvicorn サーバ）を実際に起動して持続させることはできません。
  - そのため "全部実行してみる" の実際のプロセス起動はユーザー側でお願いする必要があります。
  - 代替として、`scripts/run_simulation.py` を作成しました。これをローカルで実行すると、中央サーバとエッジサーバを順に起動し、エンドポイント応答を待って簡単な統合チェックを行います。

## 5) 次のアクション（ユーザー側での実行手順）
1. Python 環境に必要なパッケージをインストールしてください（推奨）:
   - fastapi, uvicorn, httpx, torch, aiofiles
   - 例: `pip install fastapi uvicorn httpx torch aiofiles`
2. PowerShell から以下を実行してください:
   ```powershell
   python .\scripts\run_simulation.py
   ```
   - スクリプトは `uvicorn` を使って `central_server.main:app`（ポート8000）と `edge_server.main:app`（ポート8001）を起動します。
   - 起動ログと、`/docs` と `/send_to_device` の応答を待ちます。
3. ローカルで 1 ラウンド分の流れを手でテスト：
   - 中央の `/send_model_and_app` を呼んでエッジへモデル送信
   - エッジが `/receive_model_and_app` を受け取るか確認
   - 端末モックを作り `/receive_terminal_weights/{terminal_id}` を POST して集約フローをトリガ

## 6) 変更履歴（ここに逐次追記します）
- 2025-10-22: `edge_server/utils/__init__.py` を追加（ImportError 解消のため）
- 2025-10-22: `scripts/run_simulation.py` を追加（ローカル起動支援用）
- 2025-10-22: `IMPLEMENTATION_NOTES.md` を作成（このファイル）

---

必要であれば、この `IMPLEMENTATION_NOTES.md` にさらに詳細（各関数のパッチ、PR 用の差分）を追記します。どの部分を私が次に自動で作成するか指示ください（例: `/start_round` の実装、エッジでの state マシン実装、シミュレータの拡張など）。
