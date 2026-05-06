"""
Trace ID 工具
提供在一次请求生命周期内共享 trace_id 的能力
"""
from __future__ import annotations

import contextvars
from typing import Optional

_TRACE_ID_CTX = contextvars.ContextVar("doc_studio_trace_id", default=None)


def set_trace_id(trace_id: str):
    _TRACE_ID_CTX.set(trace_id)


def get_trace_id() -> Optional[str]:
    return _TRACE_ID_CTX.get()


def clear_trace_id():
    _TRACE_ID_CTX.set(None)

