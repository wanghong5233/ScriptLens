from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from models.base import Base


class ScoringImprovementAction(Base):
    __tablename__ = "scoring_improvement_actions"

    id = Column(UUID(as_uuid=False), primary_key=True)
    run_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scoring_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    script_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    dimension = Column(String(64), nullable=False)
    signal_key = Column(String(128), nullable=False)
    template_id = Column(String(128), nullable=True)
    issue = Column(Text, nullable=False)
    target = Column(Text, nullable=False)
    action_steps = Column(JSONB, nullable=False, default=list, server_default="[]")
    evidence_refs = Column(JSONB, nullable=False, default=list, server_default="[]")
    estimated_lift = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_scoring_improvement_actions_run", "run_id"),
        Index("idx_scoring_improvement_actions_script", "script_id"),
        Index("idx_scoring_improvement_actions_dim", "dimension"),
        {"schema": "scriptlens"},
    )
