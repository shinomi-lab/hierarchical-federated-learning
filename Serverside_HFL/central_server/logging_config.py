import logging
from typing import Optional
from datetime import datetime
from pathlib import Path

def configure_logging(server_name: str, instance_name: Optional[str] = None, max_bytes: int = 10 * 1024 * 1024, backup_count: int = 3):
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
    sub_dir = base_dir / (instance_name or server_name)
    sub_dir.mkdir(parents=True, exist_ok=True)
    log_file = sub_dir / f"{server_name}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    file_handler = RotatingFileHandler(str(log_file), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    # Ensure HTTP / framework logs are also captured at INFO level so request lifecycle is visible
    try:
        logging.getLogger("uvicorn.access").setLevel(logging.INFO)
        logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    except Exception:
        pass

    logging.info(
        "ローテーション付きログを設定しました（UTF-8）: server=%s path=%s maxBytes=%s backupCount=%s",
        server_name,
        log_file,
        max_bytes,
        backup_count,
    )
