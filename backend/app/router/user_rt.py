import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

_logger = logging.getLogger(__name__)
from sqlalchemy import and_
from service.auth import create_demo_token, get_current_demo_user, get_current_user
from models.user import User as UserModel
from models.demo_access_log import DemoAccessLog
from pydantic import BaseModel
from sqlalchemy.orm import Session
from schemas.auth import LoginRequest, RegisterRequest, STSTokenRequest
from service.core.system.user_service import UserService
from core.config import settings
from utils.database import SessionLocal, get_db
from utils.rate_limiter import rate_limiter

router = APIRouter()


# 用户认证接口
@router.post("/login")
async def login(request: LoginRequest):
    """
    用户认证接口，用于登录系统。

    接收用户名和密码，通过认证后返回一个用于后续请求的JWT access token。

    - **请求体**: `LoginRequest` 模型，包含 `username` 和 `password`。
    - **成功响应**: 返回包含 `access_token` 和 `token_type` 的JSON对象。
    - **失败响应**:
        - 401 Unauthorized: 认证失败（用户名或密码错误）。
        - 500 Internal Server Error: 其他服务器内部错误。
    """
    service = UserService()
    return service.login(request)

# 用户注册接口
@router.post("/register")
async def register(request: RegisterRequest):
    """
    新用户注册接口。

    接收用户名和密码，创建新用户。如果用户名已存在，将返回错误。

    - **请求体**: `RegisterRequest` 模型，包含 `username` 和 `password`。
    - **成功响应**: 返回一个表示注册成功的消息。
    - **失败响应**:
        - 400 Bad Request: 注册失败（如用户名已存在）。
        - 500 Internal Server Error: 其他服务器内部错误。
    """
    if settings.SM_DEMO_MODE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Register is disabled in demo mode",
        )
    service = UserService()
    return service.register(request)

# STS Token 接口
# 这是一个相对独立的功能，用于获取字节跳动语音服务的临时访问凭证。
@router.post("/sts-token")
async def get_sts_token(request: STSTokenRequest):
    """
    获取字节跳动语音服务的临时安全凭证 (STS Token)。

    此接口作为一个代理，将请求转发至字节跳动的STS服务，用于获取
    客户端访问语音服务所需的临时授权。

    - **请求体**: `STSTokenRequest` 模型，包含 `appid` 和 `accessKey`。
    - **成功响应**: 直接返回字节跳动STS API的原始响应。
    - **失败响应**:
        - 408 Request Timeout: 请求STS服务超时。
        - 503 Service Unavailable: 请求STS服务失败。
        - 500 Internal Server Error: 其他服务器内部错误。
    """
    service = UserService()
    return await service.get_sts_token(request)

# 用于测试热更新的接口
@router.get("/test-hot-reload")
async def test_hot_reload():
    """一个简单的测试接口，用于验证Docker卷挂载实现的代码热更新功能。"""
    if settings.SM_DEMO_MODE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return {"message": "热更新成功！ 第3版！"}

# Pydantic模型，用于API响应
class User(BaseModel):
    id: int
    username: str

    class Config:
        orm_mode = True


class DemoEntryResponse(BaseModel):
    """Demo auto-login response."""

    access_token: str
    token_type: str = "bearer"
    username: str


class DemoEntryRequest(BaseModel):
    """Demo entry payload."""

    code: str | None = None


_DEMO_VISIT_DEDUP_SECONDS = 60


@router.post("/demo-entry", response_model=DemoEntryResponse)
async def demo_entry(
    request: Request,
    payload: DemoEntryRequest | None = None,
    code: str | None = Query(default=None, description="可选 demo 校验码（兼容旧参数）"),
    db: Session = Depends(get_db),
):
    """
    Demo 免登录入口：
    - 仅在 SM_DEMO_ENTRY_ENABLED 时可用
    - 可选校验 SM_DEMO_ENTRY_CODE
    - 返回 testuser 的短路径登录 token（与普通登录同 JWT 体系）
    """
    if not settings.SM_DEMO_ENTRY_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demo entry is disabled")

    expected_code = str(settings.SM_DEMO_ENTRY_CODE or "").strip()
    provided_code = str((payload.code if payload else None) or code or "").strip()
    if expected_code and not (
        provided_code and secrets.compare_digest(provided_code, expected_code)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid demo code")

    xff = (request.headers.get("x-forwarded-for") or "").strip()
    cfc = (request.headers.get("cf-connecting-ip") or "").strip()
    client_ip = (
        (cfc or (xff.split(",")[0].strip() if xff else ""))
        or (request.client.host if request.client else "unknown")
    ).strip() or "unknown"
    rate_bucket = f"demo-entry:{client_ip}"
    limit_per_minute = max(1, int(getattr(settings, "SM_DEMO_ENTRY_RATE_PER_MINUTE", 20) or 20))
    if not rate_limiter.check_and_consume(rate_bucket, limit=limit_per_minute, window_seconds=60):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too Many Requests")

    demo_username = str(settings.SM_DEMO_USERNAME or "testuser").strip() or "testuser"
    user = db.query(UserModel).filter(UserModel.username == demo_username).first()
    if not user or not bool(getattr(user, "is_active", True)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo user is unavailable",
        )

    user_agent = (request.headers.get("user-agent") or "")[:512]
    cutoff = datetime.utcnow() - timedelta(seconds=_DEMO_VISIT_DEDUP_SECONDS)
    existing_entry = (
        db.query(DemoAccessLog.id)
        .filter(
            and_(
                DemoAccessLog.ip == client_ip,
                DemoAccessLog.path == "(demo_entry)",
                DemoAccessLog.visited_at >= cutoff,
            )
        )
        .limit(1)
        .first()
    )
    try:
        if not existing_entry:
            # 使用独立 session 插入，避免与请求事务相互干扰
            _log_db = SessionLocal()
            try:
                _log_db.add(
                    DemoAccessLog(
                        ip=client_ip,
                        path="(demo_entry)",
                        user_agent=user_agent if user_agent.strip() else None,
                    )
                )
                _log_db.commit()
                _logger.debug("demo_entry logged: ip=%s", client_ip)
            except Exception as e:
                _log_db.rollback()
                _logger.warning("demo_entry failed to log: %s", e)
            finally:
                _log_db.close()
        else:
            _logger.debug("demo_entry dedup: ip=%s skipped", client_ip)
    except Exception as e:
        _logger.warning("demo_entry check failed: %s", e)

    return DemoEntryResponse(
        access_token=create_demo_token(user.id, user.username),
        token_type="bearer",
        username=user.username,
    )


class DemoVisitRequest(BaseModel):
    """Demo 访问记录请求。"""

    path: str


@router.post("/demo-visit")
async def demo_visit(
    request: Request,
    payload: DemoVisitRequest,
    current_user: UserModel = Depends(get_current_demo_user),
    db: Session = Depends(get_db),
):
    """
    记录 demo 用户访问的页面路径。仅限 demo token 调用。
    同一 IP + 同一 path 在 60 秒内仅记录一次，避免重复。
    """
    if not settings.SM_DEMO_ENTRY_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demo tracking disabled")

    xff = (request.headers.get("x-forwarded-for") or "").strip()
    cfc = (request.headers.get("cf-connecting-ip") or "").strip()
    client_ip = (
        (cfc or (xff.split(",")[0].strip() if xff else ""))
        or (request.client.host if request.client else "unknown")
    ).strip() or "unknown"
    path = (payload.path or "")[:512].strip() or "/"
    user_agent = (request.headers.get("user-agent") or "")[:512]

    cutoff = datetime.utcnow() - timedelta(seconds=_DEMO_VISIT_DEDUP_SECONDS)
    existing = (
        db.query(DemoAccessLog.id)
        .filter(
            and_(
                DemoAccessLog.ip == client_ip,
                DemoAccessLog.path == path,
                DemoAccessLog.visited_at >= cutoff,
            )
        )
        .limit(1)
        .first()
    )
    if existing:
        _logger.debug("demo_visit dedup: ip=%s path=%s skipped", client_ip, path)
        return {"ok": True}
    try:
        _log_db = SessionLocal()
        try:
            _log_db.add(
                DemoAccessLog(
                    ip=client_ip,
                    path=path,
                    user_agent=user_agent if user_agent.strip() else None,
                )
            )
            _log_db.commit()
            _logger.info("demo_visit logged: ip=%s path=%s", client_ip, path)
        except Exception as e:
            _log_db.rollback()
            _logger.warning("demo_visit failed to log: %s", e)
        finally:
            _log_db.close()
    except Exception as e:
        _logger.warning("demo_visit check failed: %s", e)
    return {"ok": True}


# 获取当前用户的接口
@router.get("/users/me", response_model=User)
async def read_users_me(current_user: UserModel = Depends(get_current_user)):
    """
    获取当前认证用户的个人信息。

    通过依赖注入的 `get_current_user` 函数来验证JWT，并返回
    当前用户的详细信息。

    - **依赖**: `get_current_user`，处理token验证并提供用户信息。
    - **成功响应**: 返回当前用户的 `User` 模型数据。
    - **失败响应**:
        - 401 Unauthorized: 如果token无效或已过期。
    """
    return current_user