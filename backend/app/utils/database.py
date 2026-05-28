from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models.base import Base
from core.config import settings

DATABASE_URL = settings.DATABASE_URL

if DATABASE_URL is None:
    # 整套 backend 设计为 docker compose 栈内运行（compose 注入 DATABASE_URL）。
    # 在容器外直接 `python -m cli.xxx` 会让 settings.DATABASE_URL 为 None，
    # SQLAlchemy 原始报错 "Expected string or URL object, got None" 对调用方无指导意义。
    # 把它替换成 actionable 指引；不破坏 module-level 对外 API（engine / SessionLocal 仍存在）。
    raise RuntimeError(
        "DATABASE_URL is not set. ScriptLens backend modules require running "
        "inside the docker compose stack.\n"
        "  - Start stack:  docker compose -f ScriptLens/backend/docker-compose.dev.yml up -d\n"
        "  - Run CLI:      docker exec -it scriptlens_api_dev python -m cli.<your_cli> ...\n"
        "If you really need host execution, export DATABASE_URL explicitly "
        "(e.g. postgresql://postgres:pg123456@localhost:25432/scriptlens_dev)."
    )

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """初始化数据库"""
    Base.metadata.create_all(bind=engine)