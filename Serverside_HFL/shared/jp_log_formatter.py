"""
コンソール・ファイル双方で使うログ用フォーマッタ。

- レベル名を日本語表記（デバッグ / 情報 / …）にする。メッセージ本文は呼び出し元のまま。
- ファイルは logging_config 側で encoding='utf-8' を指定すること（UTF-8 以外で開くと化けるのは読み手側の問題）。
"""

from __future__ import annotations

import logging

_LEVEL_JA = {
    "DEBUG": "デバッグ",
    "INFO": "情報",
    "WARNING": "警告",
    "ERROR": "エラー",
    "CRITICAL": "重大",
}


class JapaneseVerboseFormatter(logging.Formatter):
    """%(levelname_ja)s をレコードに付与してから整形する。"""

    def format(self, record: logging.LogRecord) -> str:
        record.levelname_ja = _LEVEL_JA.get(record.levelname, record.levelname)
        return super().format(record)
