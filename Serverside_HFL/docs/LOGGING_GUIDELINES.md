# Logging Guidelines

目的: 詳細ログを残すが、機密情報や大量データの直書きを避ける。

1. ログ対象
- HTTP リクエスト: method, path, 指定上位ヘッダ（最大20個まで）, body_preview（最大2KB）
- HTTP レスポンス: status_code, content-length, duration
- 主要内部処理: ENTER/EXIT/例外/実行時間（@log_calls デコレータ）
- ファイル操作: 保存/バックアップ/検証の成功失敗

2. マスク対象（ログに出さない/要マスク）
- Authorization ヘッダや Cookie、個人情報（名前、住所、電話番号）
- 生のバイナリデータ（画像、モデルバイナリ）はハッシュのみ出力

3. サイズ上限
- ボディは最大 `2048` バイトまでログに含める。超過時は切り詰めて `...` を付加。
- 大きなレスポンス/アップロードはバイト長と SHA256 のみをログに残す。

4. 運用推奨
- 本番環境ではログの保持期間を短く（例: 7日）に設定する。
- 個人情報が含まれる可能性のあるエンドポイントは別途監査ルールを設定。

5. 今回の実装場所
- ミドルウェアでリクエストヘッダと body_preview を取得してログ出力。
- `central_server/utils/trace_logging.py` と `edge_server/utils/trace_logging.py` にて関数レベルの ENTER/EXIT ログを追加。

