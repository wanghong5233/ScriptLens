"""Demo 访问日志，用于临时追踪 demo 展示界面的使用情况。"""

from sqlalchemy import Column, Integer, String, TIMESTAMP, Index
from sqlalchemy.sql import func

from models.base import Base


class DemoAccessLog(Base):
    """Demo 页面访问记录（简历/GitHub 等入口的体验追踪）。"""

    __tablename__ = "demo_access_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ip = Column(String(64), nullable=False, index=True, comment="客户端 IP")
    path = Column(String(512), nullable=False, comment="访问路径/URL")
    user_agent = Column(String(512), nullable=True, comment="User-Agent")
    visited_at = Column(
        TIMESTAMP,
        nullable=False,
        server_default=func.now(),
        comment="访问时间",
    )

    __table_args__ = (
        Index("idx_demo_access_logs_visited_at", "visited_at"),
        Index("idx_demo_access_logs_ip_visited", "ip", "visited_at"),
    )
