"""Add scoring v3 schema tables and columns.

Revision ID: 06_add_scoring_v3
Revises: 05_add_plot_tagging_full_schema
Create Date: 2026-05-27
"""

from typing import Sequence, Union

from alembic import op


revision: str = "06_add_scoring_v3"
down_revision: Union[str, None] = "05_add_plot_tagging_full_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.rubric_versions (
            id               UUID PRIMARY KEY,
            version          TEXT NOT NULL,
            status           TEXT NOT NULL,
            base_weight      JSONB NOT NULL DEFAULT '{}'::jsonb,
            genre_multiplier JSONB NOT NULL DEFAULT '{}'::jsonb,
            tier_cuts        JSONB NOT NULL DEFAULT '{}'::jsonb,
            signal_catalog   JSONB NOT NULL DEFAULT '{}'::jsonb,
            prompt_version   TEXT,
            model_version    TEXT,
            effective_at     TIMESTAMPTZ,
            deprecated_at    TIMESTAMPTZ,
            changelog        TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rubric_versions_version "
        "ON scriptlens.rubric_versions (version)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rubric_versions_status "
        "ON scriptlens.rubric_versions (status)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.scoring_runs (
            id              UUID PRIMARY KEY,
            script_id       UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            rubric_version  TEXT NOT NULL,
            tag_set_ver     TEXT,
            input_hash      TEXT NOT NULL,
            genre_scope     TEXT,
            episode_count   INTEGER,
            plot_unit_count INTEGER,
            quality_flags   JSONB NOT NULL DEFAULT '{}'::jsonb,
            model_versions  JSONB NOT NULL DEFAULT '{}'::jsonb,
            prompt_versions JSONB NOT NULL DEFAULT '{}'::jsonb,
            status          TEXT NOT NULL,
            error           TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scoring_runs_script_created "
        "ON scriptlens.scoring_runs (script_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scoring_runs_rubric "
        "ON scriptlens.scoring_runs (rubric_version)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scoring_runs_input_hash "
        "ON scriptlens.scoring_runs (input_hash)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.scoring_improvement_actions (
            id             UUID PRIMARY KEY,
            run_id         UUID NOT NULL REFERENCES scriptlens.scoring_runs(id) ON DELETE CASCADE,
            script_id      UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            dimension      TEXT NOT NULL,
            signal_key     TEXT NOT NULL,
            template_id    TEXT,
            issue          TEXT NOT NULL,
            target         TEXT NOT NULL,
            action_steps   JSONB NOT NULL DEFAULT '[]'::jsonb,
            evidence_refs  JSONB NOT NULL DEFAULT '[]'::jsonb,
            estimated_lift JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scoring_improvement_actions_run "
        "ON scriptlens.scoring_improvement_actions (run_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scoring_improvement_actions_script "
        "ON scriptlens.scoring_improvement_actions (script_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scoring_improvement_actions_dim "
        "ON scriptlens.scoring_improvement_actions (dimension)"
    )

    op.execute(
        """
        ALTER TABLE scriptlens.script_scores
        ADD COLUMN IF NOT EXISTS run_id UUID REFERENCES scriptlens.scoring_runs(id) ON DELETE SET NULL
        """
    )
    op.execute(
        "ALTER TABLE scriptlens.script_scores "
        "ADD COLUMN IF NOT EXISTS primary_dimension TEXT"
    )
    op.execute(
        "ALTER TABLE scriptlens.script_scores "
        "ADD COLUMN IF NOT EXISTS coverage_ratio REAL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_script_scores_run_id "
        "ON scriptlens.script_scores (run_id)"
    )

    op.execute(
        "ALTER TABLE scriptlens.reports "
        "ADD COLUMN IF NOT EXISTS decision_payload JSONB"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE scriptlens.reports "
        "DROP COLUMN IF EXISTS decision_payload"
    )

    op.execute(
        "ALTER TABLE scriptlens.script_scores "
        "DROP COLUMN IF EXISTS coverage_ratio"
    )
    op.execute(
        "ALTER TABLE scriptlens.script_scores "
        "DROP COLUMN IF EXISTS primary_dimension"
    )
    op.execute(
        "ALTER TABLE scriptlens.script_scores "
        "DROP COLUMN IF EXISTS run_id"
    )

    op.execute("DROP TABLE IF EXISTS scriptlens.scoring_improvement_actions")
    op.execute("DROP TABLE IF EXISTS scriptlens.scoring_runs")
    op.execute("DROP TABLE IF EXISTS scriptlens.rubric_versions")
