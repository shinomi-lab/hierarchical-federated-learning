"""テスト全体の共通設定。

central_server.endpoints.edge_update は import 時に init_metrics_db() を実行し、
既定では実験結果DB (received_edges/training_metrics.db) を作成/マイグレーションする。
テストが実データに触れないよう、どのテストモジュールの import よりも先に
保存先を一時ディレクトリへ向ける（HFL_STORAGE_DIR は edge_update が参照する上書き用環境変数）。
"""
import os
import tempfile

os.environ.setdefault("HFL_STORAGE_DIR", tempfile.mkdtemp(prefix="hfl_test_storage_"))
