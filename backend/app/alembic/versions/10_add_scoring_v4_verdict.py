"""scoring v4: add verdict + ad_perf_payload columns to scoring_runs.

Revision ID: 10_add_scoring_v4_verdict
Revises: 09_split_status_state
Create Date: 2026-05-31

scoring v4（docs/2026-05-31-投资决策评分框架-v4.md）需要在 scoring_runs 表新增：

- ``verdict``                v4 verdict 三档：qualified | needs_polish | not_recommended
- ``investment_score``       0-10 浮点综合分（与历史 ``overall_score`` 同义；alias 期满删除）
- ``ad_perf_payload``        投放回流数据，calibration_hook 后续离线 job 写入；本期不强制使用

历史 scoring_runs 行（v3 / Batch3）保持 ``verdict IS NULL``、``investment_score IS NULL``、
``ad_perf_payload IS NULL``，由报告渲染层根据 ``rubric_version`` 选择新旧渲染分支。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "10_add_scoring_v4_verdict"
down_revision: Union[str, None] = "09_split_status_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE scriptlens.scoring_runs
            ADD COLUMN IF NOT EXISTS verdict TEXT NULL,
            ADD COLUMN IF NOT EXISTS investment_score NUMERIC(4, 2) NULL,
            ADD COLUMN IF NOT EXISTS ad_perf_payload JSONB NULL
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'scoring_runs_verdict_check'
            ) THEN
                ALTER TABLE scriptlens.scoring_runs
                    ADD CONSTRAINT scoring_runs_verdict_check
                    CHECK (
                        verdict IS NULL
                        OR verdict IN ('qualified', 'needs_polish', 'not_recommended')
                    );
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE scriptlens.scoring_runs
            DROP CONSTRAINT IF EXISTS scoring_runs_verdict_check
        """
    )
    op.execute(
        """
        ALTER TABLE scriptlens.scoring_runs
            DROP COLUMN IF EXISTS verdict,
            DROP COLUMN IF EXISTS investment_score,
            DROP COLUMN IF EXISTS ad_perf_payload
        """
    )
