from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from models.base import Base


class ScriptScore(Base):
    __tablename__ = "script_scores"

    id = Column(UUID(as_uuid=False), primary_key=True)
    script_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scoring_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    dimension = Column(String(64), nullable=False)
    primary_dimension = Column(String(64), nullable=True)
    score = Column(Float, nullable=False)
    percentile = Column(Float, nullable=True)
    tier = Column(String(32), nullable=True)
    confidence = Column(Float, nullable=True)
    coverage_ratio = Column(Float, nullable=True)
    signals = Column(JSONB, nullable=False, default=dict, server_default="{}")
    weights = Column(JSONB, nullable=False, default=dict, server_default="{}")
    tag_set_ver = Column(String(32), nullable=False)
    score_ver = Column(String(32), nullable=False)
    model_ver = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "script_id",
            "dimension",
            "tag_set_ver",
            "score_ver",
            name="uq_script_scores_script_dim_ver",
        ),
        Index("idx_script_scores_script", "script_id"),
        Index("idx_script_scores_run_id", "run_id"),
        Index("idx_script_scores_dim_percentile", "dimension", "percentile"),
        {"schema": "scriptlens"},
    )
