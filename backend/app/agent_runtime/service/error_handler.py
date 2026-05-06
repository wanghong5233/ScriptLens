"""统一的错误处理和降级工具。"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from typing import Any, Awaitable, Callable, Optional, Tuple, Type

Logger = logging.getLogger(__name__)


def async_error_guard(
    fallback_method: str,
    *,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    log_message: Optional[str] = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """
    包装异步方法，发生异常时调用同名实例方法进行降级处理。

    Args:
        fallback_method: 降级方法在类中的名称。
        exceptions: 捕获的异常类型。
        log_message: 自定义日志。
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(func)
        async def wrapper(self, *args: Any, **kwargs: Any) -> Any:
            try:
                return await func(self, *args, **kwargs)
            except exceptions as exc:
                message = log_message or f"{func.__name__} failed: {exc}"
                logging.getLogger(func.__module__).error(message, exc_info=True)

                fallback = getattr(self, fallback_method, None)
                if fallback is None:
                    raise

                if asyncio.iscoroutinefunction(fallback):
                    return await fallback(*args, exc=exc, **kwargs)
                return fallback(*args, exc=exc, **kwargs)

        return wrapper

    return decorator


