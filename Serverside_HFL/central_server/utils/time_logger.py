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
    def __init__(self, log_dir: Optional[str] = None):
        # Default to repository's logs directory
        repo_root = Path(__file__).resolve().parent.parent
        self._base_logs = repo_root / "logs"
        self._base_logs.mkdir(parents=True, exist_ok=True)
        # Default subdir: time_records
        if log_dir is None:
            self.log_dir = self._base_logs / "time_records"
        else:
            self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.current_session = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_file = self.log_dir / f"central_server_{self.current_session}.log"

    def set_round(self, round_number: int) -> None:
        """Switch logging directory to a per-round folder: logs/rounds/r<round>."""
        try:
            rounds_dir = self._base_logs / "rounds" / f"r{int(round_number)}"
            rounds_dir.mkdir(parents=True, exist_ok=True)
            self.log_dir = rounds_dir
            self.current_session = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.session_file = self.log_dir / f"central_server_{self.current_session}.log"
        except Exception:
            # best-effort: keep previous log_dir
            pass

    def log_event(self, event_type: str, details: Optional[Dict[str, Any]] = None) -> None:
        """
        イベントとそのタイムスタンプを記録
        
        Args:
            event_type: イベントの種類（例：'model_received', 'aggregation_started'など）
            details: イベントに関する追加情報
        """
        timestamp = time.time()
        formatted_time = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")
        
        log_entry = f"[{formatted_time}] {event_type}: {json.dumps(details, ensure_ascii=False)}\n"
        
        try:
            with open(self.session_file, 'a', encoding='utf-8') as f:
                f.write(log_entry)
        except Exception as e:
            # fallback to stdout if file write fails
            print(f"⚠️ タイムスタンプの保存に失敗しました: {e}")

        # Also write to the central application logger (INFO) so that app logs contain the event
        try:
            app_logger = logging.getLogger('central_server')
            # write a concise message similar to edge logs
            app_logger.info(f"{event_type}: {json.dumps(details, ensure_ascii=False)}")
        except Exception:
            # do not raise on logging failure
            pass

# シングルトンインスタンス
central_time_logger = TimeLogger()