from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any, Dict

from loguru import logger as _logger

from core.config import settings

try:
    from pythonjsonlogger.json import JsonFormatter
except Exception:  # pragma: no cover - fallback for environments without dependency installed yet
    JsonFormatter = None  # type: ignore[assignment]


# 创建一个 ContextVar，用于在异步任务中安全地传递 request_id
request_id_var: ContextVar[str] = ContextVar("request_id", default="<no_request_id>")


def _current_trace_id() -> str:
    try:
        from agent_runtime.utils.trace import get_trace_id

        return str(get_trace_id() or "")
    except Exception:
        return ""


def _current_billing_fields() -> Dict[str, str]:
    try:
        from agent_runtime.billing_context import get_current_billing

        billing = get_current_billing()
    except Exception:
        billing = None

    if billing is None:
        return {"billing_user_id": "", "intent": ""}
    return {
        "billing_user_id": str(billing.identity.user_id or ""),
        "intent": str(billing.metadata.intent or ""),
    }


def _configure_stdlib_json_logger(log_level: str) -> None:
    if JsonFormatter is None:
        return

    handler = logging.StreamHandler(sys.stdout)
    # python-json-logger: 统一结构化字段，便于 SLS 索引（trace_id/intent/kind）
    handler.setFormatter(
        JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s "
            "%(request_id)s %(trace_id)s %(billing_user_id)s %(intent)s %(kind)s"
        )
    )
    setattr(handler, "_scriptlens_json_handler", True)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level)


def configure_logger():
    """
    配置 loguru + stdlib logging：
    - loguru 输出 JSON（兼容现有调用：log.error(..., exception=exc)）
    - stdlib logging 使用 python-json-logger 输出结构化 JSON
    """
    log_level = (settings.LOG_LEVEL or "INFO").upper()
    _configure_stdlib_json_logger(log_level)

    _logger.remove()
    _logger.add(
        sys.stdout,
        level=log_level,
        format="{message}",
        serialize=True,
        backtrace=True,
        diagnose=True,
    )

    def patch_record_with_context(record: Dict[str, Any]) -> None:
        record["extra"]["request_id"] = request_id_var.get()
        record["extra"]["trace_id"] = _current_trace_id()
        record["extra"].update(_current_billing_fields())
        record["extra"].setdefault("kind", "")

    _logger.configure(patcher=patch_record_with_context)
    return _logger


logger = configure_logger()
log = logger