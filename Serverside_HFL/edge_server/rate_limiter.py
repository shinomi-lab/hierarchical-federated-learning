"""レートリミッター（APIリクエストの頻度制限）の実装。"""
import time
from collections import defaultdict
from typing import Dict, Tuple

class RateLimiter:
    """シンプルなレートリミッター実装。"""
    def __init__(self, window_size: float = 0.001):
        """
        Args:
            window_size: 制限を適用する時間窓（秒）
        """
        self.window_size = window_size
        self._last_access: Dict[str, float] = {}
        
    def should_limit(self, key: str) -> Tuple[bool, float]:
        """
        指定したキーに対して制限すべきかを判定する。

        Returns:
            (制限すべきか, 次のリクエストまでの待ち時間)
        """
        now = time.time()
        last = self._last_access.get(key, 0)
        elapsed = now - last
        
        if elapsed < self.window_size:
            return True, max(0.1, self.window_size - elapsed)
            
        self._last_access[key] = now
        return False, 0.0