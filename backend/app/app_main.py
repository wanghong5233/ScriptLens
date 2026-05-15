import logging
import logging.config
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from core.config import settings  # <--- 关键改动：在顶部显式导入配置
from router import user_rt
from router import history_rt
from router import knowledgebase_rt
from router import document_rt
from router import job_rt
from router import session_rt
from router import config_rt
from router import internal_rt
from router import script_rt
import os
import time
import uuid
from utils.get_logger import log, request_id_var
from exceptions.base import APIException

# 从配置获取 root_path
root_path = settings.__dict__.get("ROOT_PATH", "")
service_started_at = time.time()

app = FastAPI(
    title=settings.SERVICE_DISPLAY_NAME,
    description=settings.SERVICE_DESCRIPTION,
    version=settings.SERVICE_VERSION,
    root_path=root_path,
)


class HealthAccessLogFilter(logging.Filter):
    """Filter out /health access logs to reduce noise."""

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        return "/health" not in str(record.getMessage())


uvicorn_access_logger = logging.getLogger("uvicorn.access")
if not any(isinstance(f, HealthAccessLogFilter) for f in uvicorn_access_logger.filters):
    uvicorn_access_logger.addFilter(HealthAccessLogFilter())

# 定义请求处理中间件
@app.middleware("http")
async def dispatch(request: Request, call_next):
    # 为每个请求生成唯一的 request_id
    request_id = str(uuid.uuid4())
    is_health_request = request.url.path == "/health"
    
    # 将 request_id 设置到 context var 中
    request_id_var.set(request_id)
    
    # /health 由容器频繁探活，不记录可显著降低噪音
    if not is_health_request:
        log.info(f"Request started: {request.method} {request.url.path}")
    
    start_time = time.time()
    status_code = 500
    
    try:
        response = await call_next(request)
        status_code = response.status_code
        # 在响应头中添加 request_id，方便前端调试
        response.headers["X-Request-ID"] = request_id
    except Exception as e:
        # Pass exception object directly to loguru to handle safely
        log.error("Request failed with an unhandled exception:", exception=e)
        raise e
    finally:
        process_time = (time.time() - start_time) * 1000
        if not is_health_request:
            log.info(
                f"Request finished in {process_time:.2f}ms. "
                f"Status code: {response.status_code if 'response' in locals() else status_code}"
            )

    return response

# 注册自定义API异常处理器
@app.exception_handler(APIException)
async def api_exception_handler(request: Request, exc: APIException):
    log.error(
        f"API Exception caught: {exc.message}",
        exc_info=True,
        extra={"error_code": exc.code, "status_code": exc.status_code}
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(),
        headers=exc.headers,
    )

# 注册全局未捕获异常处理器
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    # 使用安全的方式记录异常，避免字符串格式化引发 KeyError
    log.error("Unhandled exception caught:", exception=exc)
    return JSONResponse(
        status_code=500,
        content={
            "code": 50000,
            "message": "Internal Server Error",
            "data": None
        }
    )


# 添加 CORS 中间件
cors_allow_origins = [
    origin.strip()
    for origin in str(getattr(settings, "SM_CORS_ALLOW_ORIGINS", "*") or "*").split(",")
    if origin and origin.strip()
]
cors_allow_origin_regex = str(
    getattr(settings, "SM_CORS_ALLOW_ORIGIN_REGEX", "") or ""
).strip() or None
if not cors_allow_origins:
    cors_allow_origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins,
    allow_origin_regex=cors_allow_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有方法
    allow_headers=["*"],  # 允许所有头
)


@app.get("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "service": settings.SERVICE_NAME,
        "display_name": settings.SERVICE_DISPLAY_NAME,
        "version": settings.SERVICE_VERSION,
        "uptime_secs": int(time.time() - service_started_at),
    }


@app.on_event("startup")
async def llm_runtime_boot_check() -> None:
    """启动期对 LLM 可用性做 1-token 探测（详见 service.core.llm.runtime.boot_check）。

    任一 available provider 至少首位 candidate 通就放行；全部失败 → 直接抛
    LLMUnavailableError 让 startup 失败，避免运行期才发现"账号没开 qwen3-max-latest"
    这种事故再走完整 5 维报告流水线。

    设 ``LLM_BOOT_CHECK_ENABLED=false`` 可跳过（离线开发）。
    """
    if not getattr(settings, "LLM_BOOT_CHECK_ENABLED", True):
        log.info("LLMRuntime boot_check 已被 LLM_BOOT_CHECK_ENABLED=false 跳过")
        return
    try:
        from service.core.llm.runtime import LLMRuntime, LLMUnavailableError

        runtime = LLMRuntime(settings_obj=settings)
        results = await runtime.boot_check(
            probe_max_tokens=int(getattr(settings, "LLM_BOOT_CHECK_PROBE_TOKENS", 4)),
        )
        log.info(f"LLMRuntime boot_check passed: {results}")
    except Exception as e:
        if e.__class__.__name__ == "LLMUnavailableError":
            log.error(f"LLMRuntime boot_check FAILED, refusing to start: {e}")
            raise
        # 其它异常（如 settings 字段缺失）也直接抛，让启动失败比上线后才暴露好
        log.error(f"LLMRuntime boot_check unexpected error, refusing to start: {e}")
        raise

# 包含各个模块的路由，并为它们设置统一的前缀和标签
# 这有助于API文档的组织和URL的结构化
app.include_router(user_rt.router, prefix="/api/users", tags=["Users"])
app.include_router(history_rt.router, prefix="/api/history", tags=["History"])
# app.include_router(document_upload_rt.router, prefix="/api/document-upload", tags=["Document Upload"])
app.include_router(knowledgebase_rt.router, prefix="/api/knowledgebases", tags=["Knowledge Bases"])
app.include_router(document_rt.router, prefix="/api/knowledgebases/{kb_id}/documents", tags=["Documents"])
app.include_router(job_rt.router, prefix="/api/jobs", tags=["Jobs"])
app.include_router(session_rt.router, prefix="/api/sessions", tags=["Sessions"])
app.include_router(config_rt.router, prefix="/api/config", tags=["Config"])
app.include_router(internal_rt.router, prefix="/api", tags=["Internal Services"])
app.include_router(script_rt.router, prefix="/api/scripts", tags=["Scripts"])


if __name__=='__main__':
    import uvicorn
    # 在本地开发时，为了让日志配置生效，需要在这里进行配置
    # 在生产环境（如使用 Gunicorn + Uvicorn worker），通常在启动命令中配置日志
    from utils.get_logger import configure_logger
    configure_logger()
    uvicorn.run(app, host="0.0.0.0", port=8000)
    