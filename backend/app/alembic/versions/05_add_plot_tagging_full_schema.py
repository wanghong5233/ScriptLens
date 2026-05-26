"""Add full schema for plot tagging, stability runs, and scoring.

Revision ID: 05_add_plot_tagging_full_schema
Revises: 04_add_workspace_cfg
Create Date: 2026-05-26
"""

from typing import Sequence, Union

from alembic import op


revision: str = "05_add_plot_tagging_full_schema"
down_revision: Union[str, None] = "04_add_workspace_cfg"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0) 通用运行记录（被 plot/script/episode tags 外键引用）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.tag_extraction_runs (
            id            UUID PRIMARY KEY,
            script_id     UUID REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            scope         TEXT NOT NULL,            -- script|episode|plot_unit|character|relationship
            scope_id      TEXT,
            tag_set_ver   TEXT NOT NULL,
            prompt_ver    TEXT NOT NULL,
            model_ver     TEXT NOT NULL,
            seed          INTEGER,
            input_hash    TEXT NOT NULL,
            output_hash   TEXT,
            status        TEXT NOT NULL,            -- pending|success|failed
            error         TEXT,
            metrics       JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at   TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_extraction_runs_script_scope "
        "ON scriptlens.tag_extraction_runs (script_id, scope)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_extraction_runs_ver "
        "ON scriptlens.tag_extraction_runs (tag_set_ver, prompt_ver)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_extraction_runs_input_hash "
        "ON scriptlens.tag_extraction_runs (input_hash)"
    )

    # 1) LLM 缓存（input_hash 主键）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.llm_cache (
            input_hash    TEXT PRIMARY KEY,
            model_ver     TEXT NOT NULL,
            prompt_ver    TEXT,
            tag_set_ver   TEXT,
            seed          INTEGER,
            output_raw    TEXT NOT NULL,
            output_parsed JSONB NOT NULL,
            provider      TEXT NOT NULL,
            elapsed_ms    INTEGER,
            hit_count     INTEGER NOT NULL DEFAULT 0,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_hit_at   TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_cache_model_prompt_tag_seed "
        "ON scriptlens.llm_cache (model_ver, prompt_ver, tag_set_ver, seed)"
    )

    # 2) 情节单元（plot_unit）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.plot_units (
            id              UUID PRIMARY KEY,
            script_id       UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            episode_no      INTEGER,
            idx             INTEGER NOT NULL,
            start_scene_id  UUID REFERENCES scriptlens.scenes(id) ON DELETE SET NULL,
            end_scene_id    UUID REFERENCES scriptlens.scenes(id) ON DELETE SET NULL,
            start_line      INTEGER,
            end_line        INTEGER,
            summary         TEXT,
            char_count      INTEGER,
            source          TEXT NOT NULL DEFAULT 'llm',      -- llm|human_correction
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_plot_units_script_idx "
        "ON scriptlens.plot_units (script_id, idx)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_plot_units_script_episode "
        "ON scriptlens.plot_units (script_id, episode_no)"
    )

    # 3) 剧级标签（纵表）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.script_tags (
            id            UUID PRIMARY KEY,
            script_id     UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            dim           TEXT NOT NULL,
            value         TEXT NOT NULL,
            score         REAL,
            confidence    REAL,
            source        TEXT NOT NULL DEFAULT 'llm',        -- llm|human_correction|derived
            tag_set_ver   TEXT NOT NULL,
            prompt_ver    TEXT NOT NULL,
            model_ver     TEXT NOT NULL,
            run_id        UUID REFERENCES scriptlens.tag_extraction_runs(id) ON DELETE SET NULL,
            evidence      JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_script_tags_script_dim "
        "ON scriptlens.script_tags (script_id, dim)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_script_tags_dim_value "
        "ON scriptlens.script_tags (dim, value)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_script_tags_tagset_dim "
        "ON scriptlens.script_tags (tag_set_ver, dim)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_script_tags_run_id "
        "ON scriptlens.script_tags (run_id)"
    )

    # 4) 情节级标签（纵表）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.plot_unit_tags (
            id            UUID PRIMARY KEY,
            plot_unit_id  UUID NOT NULL REFERENCES scriptlens.plot_units(id) ON DELETE CASCADE,
            dim           TEXT NOT NULL,
            value         TEXT NOT NULL,
            score         REAL,
            confidence    REAL,
            source        TEXT NOT NULL DEFAULT 'llm',        -- llm|human_correction|derived
            tag_set_ver   TEXT NOT NULL,
            prompt_ver    TEXT NOT NULL,
            model_ver     TEXT NOT NULL,
            run_id        UUID REFERENCES scriptlens.tag_extraction_runs(id) ON DELETE SET NULL,
            evidence      JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_plot_unit_tags_plot_dim "
        "ON scriptlens.plot_unit_tags (plot_unit_id, dim)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_plot_unit_tags_dim_value "
        "ON scriptlens.plot_unit_tags (dim, value)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_plot_unit_tags_tagset_dim "
        "ON scriptlens.plot_unit_tags (tag_set_ver, dim)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_plot_unit_tags_run_id "
        "ON scriptlens.plot_unit_tags (run_id)"
    )

    # 5) 集级标签（纵表）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.episode_tags (
            id            UUID PRIMARY KEY,
            script_id     UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            episode_no    INTEGER NOT NULL,
            dim           TEXT NOT NULL,
            value         TEXT NOT NULL,
            score         REAL,
            confidence    REAL,
            source        TEXT NOT NULL DEFAULT 'llm',        -- llm|human_correction|derived
            tag_set_ver   TEXT NOT NULL,
            prompt_ver    TEXT NOT NULL,
            model_ver     TEXT NOT NULL,
            run_id        UUID REFERENCES scriptlens.tag_extraction_runs(id) ON DELETE SET NULL,
            evidence      JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_episode_tags_script_episode_dim "
        "ON scriptlens.episode_tags (script_id, episode_no, dim)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_episode_tags_dim_value "
        "ON scriptlens.episode_tags (dim, value)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_episode_tags_tagset_dim "
        "ON scriptlens.episode_tags (tag_set_ver, dim)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_episode_tags_run_id "
        "ON scriptlens.episode_tags (run_id)"
    )

    # 6) 人物实体
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.character_entities (
            id              UUID PRIMARY KEY,
            script_id       UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            canonical_name  TEXT NOT NULL,
            aliases         JSONB NOT NULL DEFAULT '[]'::jsonb,
            role            TEXT,
            gender          TEXT,
            archetype       TEXT,
            arc_type        TEXT,
            agency_level    TEXT,
            tag_set_ver     TEXT NOT NULL DEFAULT '',
            source          TEXT NOT NULL DEFAULT 'llm',
            evidence        JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_character_entities_script_name "
        "ON scriptlens.character_entities (script_id, canonical_name)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_character_entities_script "
        "ON scriptlens.character_entities (script_id)"
    )

    # 7) 人物关系
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.character_relationships (
            id                 UUID PRIMARY KEY,
            script_id          UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            src_char_id        UUID NOT NULL REFERENCES scriptlens.character_entities(id) ON DELETE CASCADE,
            dst_char_id        UUID NOT NULL REFERENCES scriptlens.character_entities(id) ON DELETE CASCADE,
            relationship_type  TEXT,
            polarity           TEXT,
            dynamic_arc        TEXT,
            triangle           TEXT,
            evidence           JSONB NOT NULL DEFAULT '{}'::jsonb,
            tag_set_ver        TEXT NOT NULL DEFAULT '',
            source             TEXT NOT NULL DEFAULT 'llm',
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_character_relationships_pair_ver "
        "ON scriptlens.character_relationships (src_char_id, dst_char_id, tag_set_ver)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_character_relationships_script "
        "ON scriptlens.character_relationships (script_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_character_relationships_type "
        "ON scriptlens.character_relationships (relationship_type)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_character_relationships_polarity "
        "ON scriptlens.character_relationships (polarity)"
    )

    # 8) 情节 ↔ 视频片段匹配
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.plot_unit_video_matches (
            id                UUID PRIMARY KEY,
            plot_unit_id      UUID NOT NULL REFERENCES scriptlens.plot_units(id) ON DELETE CASCADE,
            video_segment_id  TEXT NOT NULL,
            match_score       REAL,
            match_method      TEXT,
            evidence          JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_plot_unit_video_matches_plot "
        "ON scriptlens.plot_unit_video_matches (plot_unit_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_plot_unit_video_matches_video "
        "ON scriptlens.plot_unit_video_matches (video_segment_id)"
    )

    # 9) 评分结果（Batch 3 使用，本批先建）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.script_scores (
            id            UUID PRIMARY KEY,
            script_id     UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            dimension     TEXT NOT NULL,
            score         REAL NOT NULL,
            percentile    REAL,
            tier          TEXT,
            confidence    REAL,
            signals       JSONB NOT NULL DEFAULT '{}'::jsonb,
            weights       JSONB NOT NULL DEFAULT '{}'::jsonb,
            tag_set_ver   TEXT NOT NULL,
            score_ver     TEXT NOT NULL,
            model_ver     TEXT NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_script_scores_script_dim_ver "
        "ON scriptlens.script_scores (script_id, dimension, tag_set_ver, score_ver)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_script_scores_script "
        "ON scriptlens.script_scores (script_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_script_scores_dim_percentile "
        "ON scriptlens.script_scores (dimension, percentile)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scriptlens.script_scores")
    op.execute("DROP TABLE IF EXISTS scriptlens.plot_unit_video_matches")
    op.execute("DROP TABLE IF EXISTS scriptlens.character_relationships")
    op.execute("DROP TABLE IF EXISTS scriptlens.character_entities")
    op.execute("DROP TABLE IF EXISTS scriptlens.episode_tags")
    op.execute("DROP TABLE IF EXISTS scriptlens.plot_unit_tags")
    op.execute("DROP TABLE IF EXISTS scriptlens.script_tags")
    op.execute("DROP TABLE IF EXISTS scriptlens.plot_units")
    op.execute("DROP TABLE IF EXISTS scriptlens.llm_cache")
    op.execute("DROP TABLE IF EXISTS scriptlens.tag_extraction_runs")
