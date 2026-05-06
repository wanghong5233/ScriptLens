from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    ForeignKey,
    TIMESTAMP,
    JSON,
    Index,
)
from sqlalchemy.sql import func
from models.base import Base
import enum


class JobType(enum.Enum):
    INGEST_ONLINE = "ingest_online"
    UPLOAD_LOCAL = "upload_local"
    PARSE_INDEX = "parse_index"


class JobStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    PARTIAL = "partial"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Job ID")
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="Owner User ID")
    knowledge_base_id = Column(Integer, ForeignKey("knowledgebases.id", ondelete="CASCADE"), nullable=False, comment="Target KB ID")

    # 类型与状态
    type = Column(String(50), nullable=False, comment="Job Type")
    status = Column(String(50), nullable=False, default=JobStatus.PENDING.value, comment="Job Status")

    # 进度与统计
    progress = Column(Integer, nullable=False, default=0, comment="Progress 0-100")
    total = Column(Integer, nullable=False, default=0, comment="Total items")
    succeeded = Column(Integer, nullable=False, default=0, comment="Succeeded items")
    failed = Column(Integer, nullable=False, default=0, comment="Failed items")

    # 其它
    error = Column(Text, nullable=True, comment="Error message if any")
    payload = Column(JSON, nullable=True, comment="Extra payload for job params")

    # 时间戳
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now(), comment="Created at")
    updated_at = Column(TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now(), comment="Updated at")

    __table_args__ = (
        Index("idx_jobs_user_id", "user_id"),
        Index("idx_jobs_kb_id", "knowledge_base_id"),
    )

    def __repr__(self) -> str:
        return f"<Job(id={self.id}, type={self.type}, status={self.status}, kb_id={self.knowledge_base_id})>"


