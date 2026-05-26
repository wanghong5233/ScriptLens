from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from models.base import Base


class TagExtractionRun(Base):
    __tablename__ = "tag_extraction_runs"

    id = Column(UUID(as_uuid=False), primary_key=True)
    script_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scripts.id", ondelete="CASCADE"),
        nullable=True,
    )
    scope = Column(String(64), nullable=False)
    scope_id = Column(String(128), nullable=True)
    tag_set_ver = Column(String(32), nullable=False)
    prompt_ver = Column(String(64), nullable=False)
    model_ver = Column(String(128), nullable=False)
    seed = Column(Integer, nullable=True)
    input_hash = Column(String(128), nullable=False)
    output_hash = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False)
    error = Column(Text, nullable=True)
    metrics = Column(JSONB, nullable=False, default=dict, server_default="{}")
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_tag_extraction_runs_script_scope", "script_id", "scope"),
        Index("idx_tag_extraction_runs_ver", "tag_set_ver", "prompt_ver"),
        Index("idx_tag_extraction_runs_input_hash", "input_hash"),
        {"schema": "scriptlens"},
    )


class LlmCache(Base):
    __tablename__ = "llm_cache"

    input_hash = Column(String(128), primary_key=True)
    model_ver = Column(String(128), nullable=False)
    prompt_ver = Column(String(64), nullable=True)
    tag_set_ver = Column(String(32), nullable=True)
    seed = Column(Integer, nullable=True)
    output_raw = Column(Text, nullable=False)
    output_parsed = Column(JSONB, nullable=False)
    provider = Column(String(64), nullable=False)
    elapsed_ms = Column(Integer, nullable=True)
    hit_count = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_hit_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "idx_llm_cache_model_prompt_tag_seed",
            "model_ver",
            "prompt_ver",
            "tag_set_ver",
            "seed",
        ),
        {"schema": "scriptlens"},
    )
