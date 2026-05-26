from utils.database import get_db, SessionLocal
from models.user import User
from utils.password import verify_password
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from exceptions.auth import AuthError
from fastapi_jwt import JwtAccessBearerCookie
import secrets
from datetime import timedelta
import os
import logging
from fastapi import Depends, Query, HTTPException, status
from fastapi_jwt import JwtAuthorizationCredentials
from functools import lru_cache
from typing import Any, Optional

# JWT配置
from core.config import settings

# 生产级 JWT 配置
JWT_SECRET_KEY = settings.JWT_SECRET_KEY or os.environ.get('JWT_SECRET_KEY', 'CHANGE_ME_IN_PRODUCTION_' + secrets.token_hex(32))
if JWT_SECRET_KEY.startswith('CHANGE_ME'):
    logging.warning("⚠️  使用默认 JWT_SECRET_KEY，生产环境请设置环境变量！")

# 从请求头或cookie中读取访问令牌（优先从请求头读取）
access_security = JwtAccessBearerCookie(
    secret_key=JWT_SECRET_KEY,
    auto_error=True,
    access_expires_delta=timedelta(days=settings.JWT_ACCESS_TOKEN_EXPIRE_DAYS)  # 从配置读取过期时间
)

# 用于可选 token 的安全配置（不自动抛出错误）
access_security_optional = JwtAccessBearerCookie(
    secret_key=JWT_SECRET_KEY,
    auto_error=False,  # 不自动抛出错误，允许我们手动处理
    access_expires_delta=timedelta(days=settings.JWT_ACCESS_TOKEN_EXPIRE_DAYS)
)


def _parse_csv_values(raw_value: Optional[str]) -> set[str]:
    if not raw_value:
        return set()
    return {item.strip() for item in raw_value.split(",") if item and item.strip()}


class AdminConsolePrincipal:
    """独立后台登录主体（不绑定主站 users 表记录）。"""

    def __init__(self, username: str) -> None:
        self.id = 0
        self.username = username
        self.role = "super_admin"
        self.is_active = True


def _admin_console_username() -> str:
    return (settings.SM_ADMIN_CONSOLE_USERNAME or "").strip()


def _admin_console_password() -> str:
    return settings.SM_ADMIN_CONSOLE_PASSWORD or ""


def verify_admin_console_credentials(username: str, password: str) -> bool:
    expected_username = _admin_console_username()
    expected_password = _admin_console_password()
    if not expected_username or not expected_password:
        return False
    return secrets.compare_digest(username.strip(), expected_username) and secrets.compare_digest(
        password,
        expected_password,
    )


def create_admin_console_token(username: str) -> str:
    subject = {
        "user_id": 0,
        "user_name": username,
        "admin_username": username,
        "token_use": "admin_console",
        "role": "super_admin",
        "salting": secrets.token_hex(16),
    }
    return access_security.create_access_token(subject=subject)


def _build_admin_console_principal(payload: dict[str, Any]) -> Optional[AdminConsolePrincipal]:
    token_use = str(payload.get("token_use") or "").strip().lower()
    if token_use != "admin_console":
        return None
    admin_username = str(payload.get("admin_username") or payload.get("user_name") or "").strip()
    if not admin_username:
        return None
    expected_username = _admin_console_username()
    if not expected_username:
        return None
    if not secrets.compare_digest(admin_username, expected_username):
        return None
    return AdminConsolePrincipal(username=admin_username)


@lru_cache(maxsize=1)
def _admin_username_allowlist() -> set[str]:
    return {name.lower() for name in _parse_csv_values(settings.SM_ADMIN_USERNAMES)}


@lru_cache(maxsize=1)
def _admin_user_id_allowlist() -> set[int]:
    ids: set[int] = set()
    for item in _parse_csv_values(settings.SM_ADMIN_USER_IDS):
        try:
            ids.add(int(item))
        except ValueError:
            logging.warning("忽略非法 SM_ADMIN_USER_IDS 配置项: %s", item)
    return ids


@lru_cache(maxsize=1)
def _internal_service_allowlist() -> set[str]:
    return {name.lower() for name in _parse_csv_values(settings.SM_INTERNAL_SERVICE_ALLOWLIST)}


def get_user_role(user: User) -> str:
    role = (getattr(user, "role", "") or "user").strip().lower()
    if role in {"user", "admin", "super_admin"}:
        return role
    return "user"


def is_user_super_admin(user: User) -> bool:
    return get_user_role(user) == "super_admin"


def is_internal_service_token_payload(payload: dict[str, Any]) -> bool:
    token_use = str(payload.get("token_use", "") or "").strip().lower()
    if token_use != "internal_service":
        return False
    service_name = str(payload.get("service_name", "") or "").strip().lower()
    if not service_name:
        return False
    allowlist = _internal_service_allowlist()
    if allowlist and service_name not in allowlist:
        return False
    return True


def create_internal_service_token(
    *,
    service_name: str,
    acting_user_id: Optional[int] = None,
) -> str:
    normalized_name = (service_name or "").strip().lower() or "service"
    subject = {
        "user_id": int(acting_user_id or 0),
        "user_name": normalized_name,
        "service_name": normalized_name,
        "token_use": "internal_service",
        "salting": secrets.token_hex(8),
    }
    return access_security.create_access_token(subject=subject)

def create_token(user_id: int, user_name: str, salting: str = ""):
    # 生成token的主体部分，包含用户名和随机盐值
    subject = {
        "user_id": user_id,
        "user_name": user_name,
        "salting": secrets.token_hex(16)
    }
    
    # 创建新的访问令牌
    access_token = access_security.create_access_token(subject=subject)
    
    return access_token


def create_demo_token(user_id: int, user_name: str) -> str:
    """Issue short-lived token for demo entry only."""
    expire_hours = max(1, int(getattr(settings, "SM_DEMO_TOKEN_EXPIRE_HOURS", 2) or 2))
    demo_security = JwtAccessBearerCookie(
        secret_key=JWT_SECRET_KEY,
        auto_error=True,
        access_expires_delta=timedelta(hours=expire_hours),
    )
    subject = {
        "user_id": user_id,
        "user_name": user_name,
        "token_use": "demo_entry",
        "salting": secrets.token_hex(16),
    }
    return demo_security.create_access_token(subject=subject)


def authenticate(username: str, password: str) -> str:
    """
    认证用户
    
    Args:
        username (str): 用户名
        password (str): 明文密码
    
    Returns:
        str: 认证成功返回token，失败返回None
    
    Raises:
        AuthError: 认证失败时抛出
    """
    db = next(get_db())
    try:
        if _admin_console_username() and username.strip() == _admin_console_username():
            raise AuthError("该账号仅用于后台管理，请前往 /admin/login 登录")
        # 查询用户
        user = db.query(User).filter(User.username == username).first()
        
        if not user:
            raise AuthError("认证失败")
        is_active = bool(getattr(user, "is_active", True))
        if not is_active:
            raise AuthError("账号已被禁用")
        
        # 验证密码
        if not verify_password(password, user.password_hash):
            raise AuthError("认证失败")
        
        # 如果需要生成token，可以在这里实现
        # return create_token(user.id)
        return create_token(user.id, user.username)
    
    except SQLAlchemyError as e:
        raise AuthError("认证失败") from e
    finally:
        db.close()

def register_user(username: str, password: str):
    """
    注册新用户
    
    Args:
        username (str): 用户名
        password (str): 明文密码
    
    Raises:
        AuthError: 如果用户名已存在或注册失败
    """
    from utils.password import hash_password
    
    logger = logging.getLogger(__name__)
    
    logger.info(f"开始注册用户: {username}")
    db = next(get_db())
    try:
        if _admin_console_username() and username.strip() == _admin_console_username():
            raise AuthError("该用户名保留给后台管理系统")
        # 检查用户名是否已存在
        logger.info("检查用户名是否已存在...")
        existing_user = db.query(User).filter(User.username == username).first()
        if existing_user:
            logger.warning(f"用户名 {username} 已存在")
            raise AuthError("用户名已存在")
        
        # 对密码进行哈希处理
        logger.info("开始密码哈希处理...")
        password_hash = hash_password(password)
        logger.info("密码哈希处理完成")
        
        # 创建新用户
        logger.info("创建新用户记录...")
        new_user = User(username=username, password_hash=password_hash)
        db.add(new_user)
        
        # 提交事务
        logger.info("提交数据库事务...")
        db.commit()
        logger.info(f"用户 {username} 注册成功")
        
    except SQLAlchemyError as e:
        logger.error(f"数据库操作失败: {str(e)}")
        db.rollback()
        raise AuthError(f"注册失败: {str(e)}")
    except Exception as e:
        logger.error(f"注册过程中发生未知错误: {str(e)}")
        db.rollback()
        raise AuthError(f"注册失败: {str(e)}")
    finally:
        db.close()
        logger.info("数据库连接已关闭")

def get_current_demo_user(subject: "JwtAuthorizationCredentials" = Depends(access_security)):
    """仅接受 demo_entry token，用于 demo-visit 等仅限 demo 用户的接口。"""
    payload = subject.subject or {}
    token_use = str(payload.get("token_use") or "").strip().lower()
    if token_use != "demo_entry":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Demo token required",
        )
    db = next(get_db())
    try:
        user_id = payload.get("user_id")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: user_id missing",
            )
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
            )
        is_active = bool(getattr(user, "is_active", True))
        if not is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is disabled",
            )
        return user
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}",
        ) from e
    finally:
        db.close()


def get_current_user(subject: "JwtAuthorizationCredentials" = Depends(access_security)):
    """
    FastAPI 依赖项，用于获取当前认证的用户。
    它会验证JWT，并从数据库中检索用户信息。
    """
    db = next(get_db())
    try:
        # subject 对象本身不是字典，我们需要访问它的 'subject' 属性来获取 payload
        payload = subject.subject
        user_id = payload.get("user_id")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: user_id missing",
            )
        
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
            )
        is_active = bool(getattr(user, "is_active", True))
        if not is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is disabled",
            )
        
        token_use = str(payload.get("token_use") or "").strip().lower()
        setattr(user, "_token_use", token_use)
        return user
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}",
        )
    finally:
        db.close()


def is_user_admin(user: User) -> bool:
    role = get_user_role(user)
    if role in {"admin", "super_admin"}:
        return True
    username_allowlist = _admin_username_allowlist()
    if "*" in username_allowlist:
        return True
    username = (getattr(user, "username", "") or "").strip().lower()
    user_id = getattr(user, "id", None)
    if username and username in username_allowlist:
        return True
    if user_id is not None:
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            user_id_int = None
        if user_id_int is not None and user_id_int in _admin_user_id_allowlist():
            return True
    return False


def get_current_admin_user(
    subject: "JwtAuthorizationCredentials" = Depends(access_security),
    db: Session = Depends(get_db),
) -> User | AdminConsolePrincipal:
    """管理员鉴权：优先独立 admin_console token，兼容历史 admin user token。"""
    payload = subject.subject or {}
    admin_console_principal = _build_admin_console_principal(payload)
    if admin_console_principal is not None:
        # 为兼容依赖 User 实例的历史管理接口，优先映射到白名单中的真实用户。
        mapped_admin_ids = sorted(_admin_user_id_allowlist())
        if mapped_admin_ids:
            mapped_user = db.query(User).filter(User.id == mapped_admin_ids[0]).first()
            if mapped_user and bool(getattr(mapped_user, "is_active", True)):
                return mapped_user
        return admin_console_principal

    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token: user_id missing",
        )
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    is_active = bool(getattr(user, "is_active", True))
    if not is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is disabled",
        )
    if is_user_admin(user):
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access required",
    )


def get_current_admin_console_user(
    subject: "JwtAuthorizationCredentials" = Depends(access_security),
) -> AdminConsolePrincipal:
    """仅接受独立后台 token。"""
    payload = subject.subject or {}
    principal = _build_admin_console_principal(payload)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin console token required",
        )
    return principal


def get_current_user_optional_query_token(
    token: Optional[str] = Query(None, description="JWT token for authentication"),
    subject: Optional["JwtAuthorizationCredentials"] = Depends(access_security_optional)
):
    """
    支持从查询参数或 Authorization header 中读取 token 的认证依赖。
    优先使用 header 中的 token，如果不存在则尝试查询参数。
    用于需要在新标签页打开的场景（如 PDF 预览）。
    """
    db = next(get_db())
    try:
        # 优先使用 header 中的 token
        if subject is not None:
            payload = subject.subject
            user_id = payload.get("user_id")
        # 如果 header 中没有 token，尝试从查询参数解析
        elif token:
            try:
                # 手动解析 token
                from jose import jwt, JWTError
                decoded = jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
                # JWT payload 结构: {"subject": {"user_id": 1, "user_name": "...", ...}, ...}
                payload = decoded.get("subject", {})
                user_id = payload.get("user_id")
            except JWTError as e:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Invalid token: {str(e)}"
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Credentials are not provided"
            )
        
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: user_id not found"
            )
        
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found"
            )
        is_active = bool(getattr(user, "is_active", True))
        if not is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is disabled",
            )
        
        token_use = str((payload or {}).get("token_use") or "").strip().lower()
        setattr(user, "_token_use", token_use)
        return user
    finally:
        db.close()