import sys
from contextvars import ContextVar
from loguru import logger
from core.config import settings

# 创建一个 ContextVar，用于在异步任务中安全地传递 request_id
request_id_var: ContextVar[str] = ContextVar("request_id", default="<no_request_id>")

def configure_logger():
    """
    配置 loguru 日志记录器。
    - 输出 JSON，附带 request_id
    - 日志级别来自 settings
    """
    logger.remove()
    log_level = (settings.LOG_LEVEL or "INFO").upper()
    logger.add(
        sys.stderr,
        level=log_level,
        format="{message}",
        serialize=True,
        backtrace=True,
        diagnose=True,
    )

    def patch_record_with_request_id(record):
        record["extra"]["request_id"] = request_id_var.get()

    logger.configure(patcher=patch_record_with_request_id)
    return logger

log = configure_logger()