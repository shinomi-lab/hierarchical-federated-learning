"""Edge server のロギング設定を管理するモジュール"""

import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

def configure_logging(server_name: str, edge_id: Optional[str] = None, max_bytes: int = 10 * 1024 * 1024, backup_count: int = 3):
    """ローテーション付きファイル + コンソール出力を設定する。

    - ルートロガーにハンドラを設定（既存ハンドラは一旦解除）
    - 出力先は `logs/time_records/<edge_id or server_name>/<server_name>.log`
    - HTTP アクセス/エラーは INFO レベルで出力
    """
    # For detailed tracing enable HTTP access logs at INFO (but avoid DEBUG spam)
    try:
        logging.getLogger("uvicorn.access").setLevel(logging.INFO)
        logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    except Exception:
        pass

    # ログディレクトリとファイル名を設定（エッジIDで分離）
    from logging.handlers import RotatingFileHandler

    try:
        from shared.jp_log_formatter import JapaneseVerboseFormatter as _FmtCls
        _fmt = (
            "%(asctime)s [%(levelname_ja)s] %(name)s - %(module)s.%(funcName)s:%(lineno)d - %(message)s"
        )
        fmt = _FmtCls(_fmt, datefmt="%Y-%m-%d %H:%M:%S")
    except Exception:
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s - %(module)s.%(funcName)s:%(lineno)d - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    base_dir = Path("logs") / "time_records"
    sub_dir = base_dir / (edge_id or server_name)
    sub_dir.mkdir(parents=True, exist_ok=True)
    log_file = sub_dir / f"{server_name}.log"

    # ルートロガーを設定
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # 既存ハンドラを簡易クリーンアップ（過剰重複を避ける）
    # 注意: 他所でハンドラ追加している場合は影響に留意
    for h in list(root.handlers):
        root.removeHandler(h)

    file_handler = RotatingFileHandler(str(log_file), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # コンソールにも出力（必要に応じて）
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    logging.info(
        "ローテーション付きログを設定しました（UTF-8）: server=%s path=%s maxBytes=%s backupCount=%s",
        server_name,
        log_file,
        max_bytes,
        backup_count,
    )