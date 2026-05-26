import logging
import time
import uuid

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.config import settings
from exceptions.base import APIException
from router import config_rt
from router import document_rt
from router import history_rt
from router import internal_rt
from router import job_rt
from router import knowledgebase_rt
from router import script_rt
from router import session_rt
from router import user_rt
from utils.get_logger import log, request_id_var

DEFAULT_ERROR_STATUS_CODE = status.HTTP_500_INTERNAL_SERVER_ERROR
DEFAULT_INTERNAL_ERROR_CODE = 50000
MILLISECONDS_PER_SECOND = 1000
UVICORN_DEFAULT_PORT = 8000
UVICORN_DEFAULT_HOST = "0.0.0.0"

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

for logger_name in ("httpx", "httpcore", "openai", "urllib3"):
    logging.getLogger(logger_name).setLevel(logging.WARNING)


@app.middleware("http")
async def dispatch(request: Request, call_next):
    request_id = str(uuid.uuid4())
    is_health_request = request.url.path == "/health"
    request_id_var.set(request_id)

    if not is_health_request:
        log.debug(f"Request started: {request.method} {request.url.path}")

    start_time = time.time()
    status_code = DEFAULT_ERROR_STATUS_CODE

    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
    except Exception as exc:
        log.error("Request failed with an unhandled exception:", exception=exc)
        raise exc
    finally:
        process_time = (time.time() - start_time) * MILLISECONDS_PER_SECOND
        if not is_health_request:
            log.debug(
                f"Request finished in {process_time:.2f}ms. "
                f"Status code: {response.status_code if 'response' in locals() else status_code}"
            )

    return response


@app.exception_handler(APIException)
async def api_exception_handler(request: Request, exc: APIException):
    log.error(
        f"API Exception caught: {exc.message}",
        exc_info=True,
        extra={"error_code": exc.code, "status_code": exc.status_code},
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(),
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    log.error("Unhandled exception caught:", exception=exc)
    return JSONResponse(
        status_code=DEFAULT_ERROR_STATUS_CODE,
        content={
            "code": DEFAULT_INTERNAL_ERROR_CODE,
            "message": "Internal Server Error",
            "data": None,
        },
    )


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
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": settings.SERVICE_NAME,
        "display_name": settings.SERVICE_DISPLAY_NAME,
        "version": settings.SERVICE_VERSION,
        "uptime_secs": int(time.time() - service_started_at),
    }


@app.on_event("startup")
async def llm_runtime_boot_check() -> None:
    if not getattr(settings, "LLM_BOOT_CHECK_ENABLED", True):
        log.info("LLMRuntime boot_check skipped by LLM_BOOT_CHECK_ENABLED=false")
        return

    try:
        from service.core.llm.runtime import LLMRuntime

        runtime = LLMRuntime(settings_obj=settings)
        results = await runtime.boot_check(
            probe_max_tokens=int(getattr(settings, "LLM_BOOT_CHECK_PROBE_TOKENS", 4)),
        )
        log.info(f"LLMRuntime boot_check passed: {results}")
    except Exception as exc:
        if exc.__class__.__name__ == "LLMUnavailableError":
            log.error(f"LLMRuntime boot_check FAILED, refusing to start: {exc}")
            raise
        log.error(f"LLMRuntime boot_check unexpected error, refusing to start: {exc}")
        raise


app.include_router(user_rt.router, prefix="/api/users", tags=["Users"])
app.include_router(history_rt.router, prefix="/api/history", tags=["History"])
app.include_router(knowledgebase_rt.router, prefix="/api/knowledgebases", tags=["Knowledge Bases"])
app.include_router(document_rt.router, prefix="/api/knowledgebases/{kb_id}/documents", tags=["Documents"])
app.include_router(job_rt.router, prefix="/api/jobs", tags=["Jobs"])
app.include_router(session_rt.router, prefix="/api/sessions", tags=["Sessions"])
app.include_router(config_rt.router, prefix="/api/config", tags=["Config"])
app.include_router(internal_rt.router, prefix="/api", tags=["Internal Services"])
app.include_router(script_rt.router, prefix="/api/scripts", tags=["Scripts"])


if __name__ == "__main__":
    import uvicorn
    from utils.get_logger import configure_logger

    configure_logger()
    uvicorn.run(app, host=UVICORN_DEFAULT_HOST, port=UVICORN_DEFAULT_PORT)
