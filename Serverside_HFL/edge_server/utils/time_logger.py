"""
タイムスタンプを記録するためのユーティリティモジュール
"""
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
import logging

class TimeLogger:
    def __init__(self, edge_id: str, log_dir: str = "logs/time_records"):
        self.edge_id = edge_id
        # base log root (repo root / logs)
        self._repo_root = Path(__file__).resolve().parent.parent.parent
        self._base_logs = self._repo_root / "logs"
        self._base_logs.mkdir(parents=True, exist_ok=True)
        # default subdir
        self.log_dir = self._base_logs / Path(log_dir).name
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.current_session = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_file = self.log_dir / f"edge_server_{edge_id}_{self.current_session}.log"
        self._logger = logging.getLogger("edge.time_logger")
        self._logger.setLevel(logging.DEBUG)

    def set_round(self, round_number: int) -> None:
        """Switch logging directory to a per-round folder: logs/rounds/r<round>.

        Subsequent log_event writes will append to a new session file inside that folder.
        """
        try:
            rounds_dir = self._base_logs / "rounds" / f"r{int(round_number)}"
            rounds_dir.mkdir(parents=True, exist_ok=True)
            self.log_dir = rounds_dir
            # start a new session file to avoid mixing rounds in the same file
            self.current_session = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.session_file = self.log_dir / f"edge_server_{self.edge_id}_{self.current_session}.log"
        except Exception:
            # best-effort: keep previous log_dir
            pass

    def log_event(self, event_type: str, details: Optional[Dict[str, Any]] = None, level: str = "info") -> None:
        """
        イベントとそのタイムスタンプを記録
        
        Args:
            event_type: イベントの種類（例：'weights_received', 'aggregation_started'など）
            details: イベントに関する追加情報
        """
        timestamp = time.time()
        formatted_time = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")
        details = details or {}
        entry = {"ts": formatted_time, "level": level, "event": event_type, "details": details}
        log_entry = json.dumps(entry, ensure_ascii=False)

        # write to file
        try:
            with open(self.session_file, 'a', encoding='utf-8') as f:
                f.write(log_entry + "\n")
        except Exception as e:
            # fallback to console if file write fails
            try:
                print(f"⚠️ タイムスタンプの保存に失敗しました: {e}")
            except Exception:
                pass

        # also emit to python logger
        try:
            msg = f"{event_type} - {details}"
            if level.lower() in ("error", "err", "e"):
                self._logger.error(msg)
            elif level.lower() in ("warn", "warning"):
                self._logger.warning(msg)
            else:
                self._logger.info(msg)
        except Exception:
            pass

    def log_info(self, event_type: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.log_event(event_type, details, level="info")

    def log_warn(self, event_type: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.log_event(event_type, details, level="warning")

    def log_error(self, event_type: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.log_event(event_type, details, level="error")

# シングルトンインスタンス（EDGE_SERVER_IDは設定ファイルから取得）
from edge_server.config import EDGE_SERVER_ID
edge_time_logger = TimeLogger(EDGE_SERVER_ID)