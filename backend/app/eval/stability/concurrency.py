"""AIMD 自适应并发控制器，用于稳定性实验的 LLM 调用。

DashScope 不暴露"事先查询并发配额"的 API，工程上只能阶梯式试探：
- 起步 `start` 个并发槽位；
- 遇到限流（RateLimitError / 429）时并发减半，并对当次调用指数退避重试；
- 在 `ramp_idle_s` 秒内未再次触发限流，则并发 +2，上限 `hi`。

Semaphore 在 concurrency 调整时整体替换：已经 acquire 的协程持有旧 sem
的"凭证"，释放时回到旧 sem（在仍持有的协程范围内允许）；新的 acquire 走
新 sem，从而平滑过渡，不会瞬时把已经在跑的任务踢出。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, TypeVar

from openai import RateLimitError

T = TypeVar("T")


class AIMD:
    def __init__(
        self,
        *,
        start: int = 16,
        lo: int = 4,
        hi: int = 32,
        ramp_idle_s: float = 300.0,
    ) -> None:
        self._n = max(lo, min(hi, start))
        self._lo, self._hi = lo, hi
        self._ramp_idle_s = ramp_idle_s
        self._sem: asyncio.Semaphore = asyncio.Semaphore(self._n)
        self._last_event_ts = time.time()
        self._lock = asyncio.Lock()
        self._rate_limit_hits = 0
        self._total_calls = 0

    @property
    def concurrency(self) -> int:
        return self._n

    def stats(self) -> dict[str, int]:
        return {
            "concurrency": self._n,
            "rate_limit_hits": self._rate_limit_hits,
            "total_calls": self._total_calls,
        }

    async def acquire(self) -> asyncio.Semaphore:
        # 返回当前 sem 引用，调用方释放到同一个实例上；防止 ramp 后 release 串到新 sem
        current = self._sem
        await current.acquire()
        self._total_calls += 1
        return current

    @staticmethod
    def release(sem: asyncio.Semaphore) -> None:
        sem.release()

    async def on_rate_limit(self) -> None:
        async with self._lock:
            self._rate_limit_hits += 1
            self._last_event_ts = time.time()
            new_n = max(self._lo, self._n // 2)
            if new_n != self._n:
                self._n = new_n
                self._sem = asyncio.Semaphore(self._n)
                print(f"[aimd] 429 hit -> concurrency={self._n}", flush=True)

    async def maybe_ramp(self) -> None:
        async with self._lock:
            now = time.time()
            if self._n < self._hi and (now - self._last_event_ts) >= self._ramp_idle_s:
                self._n = min(self._hi, self._n + 2)
                self._sem = asyncio.Semaphore(self._n)
                self._last_event_ts = now
                print(f"[aimd] ramp -> concurrency={self._n}", flush=True)


_RATE_LIMIT_MARKERS = ("ratelimit", "rate limit", "429", "too many requests")


def _looks_like_rate_limit(exc: BaseException) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


async def aimd_call(
    aimd: AIMD,
    coro_factory: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 6,
    backoff_base: float = 2.0,
    backoff_max: float = 60.0,
) -> T:
    """在 AIMD 限流下跑一次任务。限流异常 → 退避重试 + 降并发；其他异常直接抛。"""
    attempt = 0
    while True:
        sem = await aimd.acquire()
        try:
            return await coro_factory()
        except Exception as exc:
            if not _looks_like_rate_limit(exc):
                raise
            attempt += 1
            await aimd.on_rate_limit()
            if attempt > max_retries:
                raise
        finally:
            AIMD.release(sem)
        wait = min(backoff_max, backoff_base ** attempt) * (0.9 + random.random() * 0.2)
        await asyncio.sleep(wait)


async def aimd_ramp_watcher(aimd: AIMD, stop_event: asyncio.Event, *, interval_s: float = 60.0) -> None:
    """后台协程：定期触发 ramp 检查。主流程结束时 set stop_event 即可优雅退出。"""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            return
        except asyncio.TimeoutError:
            await aimd.maybe_ramp()
