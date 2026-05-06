from __future__ import annotations
import time
import threading
from typing import Dict, Tuple


class InMemoryRateLimiter:
    """
    简易进程内限流器（占位实现）：
    - 基于 (bucket_key, window_start) 计数；到期自动翻窗
    - 非分布式，仅用于开发阶段；生产建议使用 Redis/网关限流
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> (window_start_epoch, count)
        self._buckets: Dict[str, Tuple[int, int]] = {}

    def check_and_consume(self, key: str, limit: int, window_seconds: int) -> bool:
        now = int(time.time())
        window_start = now - (now % window_seconds)
        with self._lock:
            start, cnt = self._buckets.get(key, (window_start, 0))
            if start != window_start:
                start, cnt = window_start, 0
            if cnt >= limit:
                # 拒绝
                self._buckets[key] = (start, cnt)
                return False
            cnt += 1
            self._buckets[key] = (start, cnt)
            return True


# 单例实例（进程内）
rate_limiter = InMemoryRateLimiter()


