from sqlalchemy import Column, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from models.base import Base


class RubricVersion(Base):
    __tablename__ = "rubric_versions"

    id = Column(UUID(as_uuid=False), primary_key=True)
    version = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False)
    base_weight = Column(JSONB, nullable=False, default=dict, server_default="{}")
    genre_multiplier = Column(JSONB, nullable=False, default=dict, server_default="{}")
    tier_cuts = Column(JSONB, nullable=False, default=dict, server_default="{}")
    signal_catalog = Column(JSONB, nullable=False, default=dict, server_default="{}")
    prompt_version = Column(String(64), nullable=True)
    model_version = Column(String(128), nullable=True)
    effective_at = Column(DateTime(timezone=True), nullable=True)
    deprecated_at = Column(DateTime(timezone=True), nullable=True)
    changelog = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("uq_rubric_versions_version", "version", unique=True),
        Index("idx_rubric_versions_status", "status"),
        {"schema": "scriptlens"},
    )
