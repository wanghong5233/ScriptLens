from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from models.base import Base


class ScoringRun(Base):
    __tablename__ = "scoring_runs"

    id = Column(UUID(as_uuid=False), primary_key=True)
    script_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    rubric_version = Column(String(32), nullable=False)
    tag_set_ver = Column(String(32), nullable=True)
    input_hash = Column(String(128), nullable=False)
    genre_scope = Column(String(64), nullable=True)
    episode_count = Column(Integer, nullable=True)
    plot_unit_count = Column(Integer, nullable=True)
    quality_flags = Column(JSONB, nullable=False, default=dict, server_default="{}")
    model_versions = Column(JSONB, nullable=False, default=dict, server_default="{}")
    prompt_versions = Column(JSONB, nullable=False, default=dict, server_default="{}")
    status = Column(String(32), nullable=False)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_scoring_runs_script_created", "script_id", "created_at"),
        Index("idx_scoring_runs_rubric", "rubric_version"),
        Index("idx_scoring_runs_input_hash", "input_hash"),
        {"schema": "scriptlens"},
    )
