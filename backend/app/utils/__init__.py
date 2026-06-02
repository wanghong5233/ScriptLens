# -*- coding: utf-8 -*-
# from .lazy_loader import LazyLoader
# from .singleton import singleton
from .get_logger import log as logger
from typing import Any


def get_db(*args: Any, **kwargs: Any):
    # 延迟导入，避免仅导入 utils 包时强依赖 DATABASE_URL 已配置
    from .database import get_db as _get_db

    return _get_db(*args, **kwargs)


class _SessionLocalProxy:
    def __call__(self, *args: Any, **kwargs: Any):
        from .database import SessionLocal as _session_local

        return _session_local(*args, **kwargs)

    def __getattr__(self, item: str):
        from .database import SessionLocal as _session_local

        return getattr(_session_local, item)


SessionLocal = _SessionLocalProxy()

__all__ = [
    "LazyLoader",
    "singleton",
    "get_db",
    "SessionLocal",
    "logger"
]