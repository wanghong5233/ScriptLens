from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from models.base import Base


class PlotUnit(Base):
    __tablename__ = "plot_units"

    id = Column(UUID(as_uuid=False), primary_key=True)
    script_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    episode_no = Column(Integer, nullable=True)
    idx = Column(Integer, nullable=False)
    start_scene_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scenes.id", ondelete="SET NULL"),
        nullable=True,
    )
    end_scene_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scenes.id", ondelete="SET NULL"),
        nullable=True,
    )
    start_line = Column(Integer, nullable=True)
    end_line = Column(Integer, nullable=True)
    summary = Column(Text, nullable=True)
    char_count = Column(Integer, nullable=True)
    source = Column(String(32), nullable=False, default="llm", server_default="llm")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("script_id", "idx", name="uq_plot_units_script_idx"),
        Index("idx_plot_units_script_episode", "script_id", "episode_no"),
        {"schema": "scriptlens"},
    )


class PlotUnitTag(Base):
    __tablename__ = "plot_unit_tags"

    id = Column(UUID(as_uuid=False), primary_key=True)
    plot_unit_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.plot_units.id", ondelete="CASCADE"),
        nullable=False,
    )
    dim = Column(String(128), nullable=False)
    value = Column(Text, nullable=False)
    score = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)
    source = Column(String(32), nullable=False, default="llm", server_default="llm")
    tag_set_ver = Column(String(32), nullable=False)
    prompt_ver = Column(String(64), nullable=False)
    model_ver = Column(String(128), nullable=False)
    run_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.tag_extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    evidence = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_plot_unit_tags_plot_dim", "plot_unit_id", "dim"),
        Index("idx_plot_unit_tags_dim_value", "dim", "value"),
        Index("idx_plot_unit_tags_tagset_dim", "tag_set_ver", "dim"),
        Index("idx_plot_unit_tags_run_id", "run_id"),
        {"schema": "scriptlens"},
    )


class ScriptTag(Base):
    __tablename__ = "script_tags"

    id = Column(UUID(as_uuid=False), primary_key=True)
    script_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    dim = Column(String(128), nullable=False)
    value = Column(Text, nullable=False)
    score = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)
    source = Column(String(32), nullable=False, default="llm", server_default="llm")
    tag_set_ver = Column(String(32), nullable=False)
    prompt_ver = Column(String(64), nullable=False)
    model_ver = Column(String(128), nullable=False)
    run_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.tag_extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    evidence = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_script_tags_script_dim", "script_id", "dim"),
        Index("idx_script_tags_dim_value", "dim", "value"),
        Index("idx_script_tags_tagset_dim", "tag_set_ver", "dim"),
        Index("idx_script_tags_run_id", "run_id"),
        {"schema": "scriptlens"},
    )


class EpisodeTag(Base):
    __tablename__ = "episode_tags"

    id = Column(UUID(as_uuid=False), primary_key=True)
    script_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    episode_no = Column(Integer, nullable=False)
    dim = Column(String(128), nullable=False)
    value = Column(Text, nullable=False)
    score = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)
    source = Column(String(32), nullable=False, default="llm", server_default="llm")
    tag_set_ver = Column(String(32), nullable=False)
    prompt_ver = Column(String(64), nullable=False)
    model_ver = Column(String(128), nullable=False)
    run_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.tag_extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    evidence = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_episode_tags_script_episode_dim", "script_id", "episode_no", "dim"),
        Index("idx_episode_tags_dim_value", "dim", "value"),
        Index("idx_episode_tags_tagset_dim", "tag_set_ver", "dim"),
        Index("idx_episode_tags_run_id", "run_id"),
        {"schema": "scriptlens"},
    )


class CharacterEntity(Base):
    __tablename__ = "character_entities"

    id = Column(UUID(as_uuid=False), primary_key=True)
    script_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    canonical_name = Column(String(256), nullable=False)
    aliases = Column(JSONB, nullable=False, default=list, server_default="[]")
    role = Column(String(128), nullable=True)
    gender = Column(String(64), nullable=True)
    archetype = Column(String(128), nullable=True)
    arc_type = Column(String(128), nullable=True)
    agency_level = Column(String(64), nullable=True)
    tag_set_ver = Column(String(32), nullable=False, default="", server_default="")
    source = Column(String(32), nullable=False, default="llm", server_default="llm")
    evidence = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("script_id", "canonical_name", name="uq_character_entities_script_name"),
        Index("idx_character_entities_script", "script_id"),
        {"schema": "scriptlens"},
    )


class CharacterRelationship(Base):
    __tablename__ = "character_relationships"

    id = Column(UUID(as_uuid=False), primary_key=True)
    script_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.scripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    src_char_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.character_entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    dst_char_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.character_entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_type = Column(String(128), nullable=True)
    polarity = Column(String(64), nullable=True)
    dynamic_arc = Column(String(128), nullable=True)
    triangle = Column(String(128), nullable=True)
    evidence = Column(JSONB, nullable=False, default=dict, server_default="{}")
    tag_set_ver = Column(String(32), nullable=False, default="", server_default="")
    source = Column(String(32), nullable=False, default="llm", server_default="llm")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "src_char_id",
            "dst_char_id",
            "tag_set_ver",
            name="uq_character_relationships_pair_ver",
        ),
        Index("idx_character_relationships_script", "script_id"),
        Index("idx_character_relationships_type", "relationship_type"),
        Index("idx_character_relationships_polarity", "polarity"),
        {"schema": "scriptlens"},
    )


class PlotUnitVideoMatch(Base):
    __tablename__ = "plot_unit_video_matches"

    id = Column(UUID(as_uuid=False), primary_key=True)
    plot_unit_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scriptlens.plot_units.id", ondelete="CASCADE"),
        nullable=False,
    )
    video_segment_id = Column(String(256), nullable=False)
    match_score = Column(Float, nullable=True)
    match_method = Column(String(64), nullable=True)
    evidence = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_plot_unit_video_matches_plot", "plot_unit_id"),
        Index("idx_plot_unit_video_matches_video", "video_segment_id"),
        {"schema": "scriptlens"},
    )
