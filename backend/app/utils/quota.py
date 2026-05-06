from __future__ import annotations
import time
import threading
from typing import Dict, Tuple


class InMemoryQuota:
    """
    简易进程内配额器：支持计数与字节额度，按时间窗滚动。
    仅用于开发阶段，生产建议改为 Redis/DB。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> (window_start_epoch, count)
        self._cnt: Dict[str, Tuple[int, int]] = {}
        # key -> (window_start_epoch, bytes)
        self._bytes: Dict[str, Tuple[int, int]] = {}

    def consume_count(self, key: str, limit: int, window_seconds: int) -> bool:
        now = int(time.time())
        window_start = now - (now % window_seconds)
        with self._lock:
            start, cnt = self._cnt.get(key, (window_start, 0))
            if start != window_start:
                start, cnt = window_start, 0
            if cnt + 1 > limit:
                self._cnt[key] = (start, cnt)
                return False
            self._cnt[key] = (start, cnt + 1)
            return True

    def consume_bytes(self, key: str, amount: int, limit: int, window_seconds: int) -> bool:
        now = int(time.time())
        window_start = now - (now % window_seconds)
        with self._lock:
            start, used = self._bytes.get(key, (window_start, 0))
            if start != window_start:
                start, used = window_start, 0
            if used + amount > limit:
                self._bytes[key] = (start, used)
                return False
            self._bytes[key] = (start, used + amount)
            return True


quota = InMemoryQuota()


