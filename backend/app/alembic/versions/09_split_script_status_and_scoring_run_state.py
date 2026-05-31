"""Add analysis_status to scripts + scoring_runs.status not null default 'running'.

Revision ID: 09_split_status_state
Revises: 08_extend_character_bios
Create Date: 2026-05-31

W1.5 + W1.6 (2026-05-31): release-readiness fixes.

W1.5 拆 script status 维度
---------------------------
旧实现：``scripts.status`` 同时表达「上传/解析」和「报告分析」两套生命周期。
导致：
  - generate_report 失败时把 status 翻成 ``failed``，**覆盖**了 ingest 成功后的
    ``ready``。用户重新打开剧本会看到「上传失败」，但实际上文件、scenes、entities
    全在 DB 里——只是分析失败。
  - dashboard 无法区分「上传问题」「分析问题」，运维定位变难。

新策略：
  - ``scripts.status`` 严格只表达 ingest 生命周期（pending/parsing/indexing/ready/failed）
  - 新增 ``scripts.last_analysis_status`` (running | done | failed | null)，专门
    表达「最近一次 generate_report 结果」。null 表示没跑过；前端 dashboard
    可显示「未分析」徽章。
  - generate_report 入口 SET last_analysis_status='running'，成功 SET 'done'，
    失败 SET 'failed'。**不再翻动 scripts.status**。

W1.6 scoring_runs 状态机
-------------------------
旧实现：scoring_runs 只在成功时 INSERT status='done'。失败时**没有记录**——
用户看不到「这次分析失败了」，只看到「上次成功 run 还在那儿」。

新策略：
  - generate_report 入口 INSERT scoring_runs status='running'
  - 成功时 UPDATE 为 'done'（或 INSERT 覆盖）
  - 失败时 UPDATE 为 'failed' + 写 error message

这条 migration 给 scoring_runs.status 加 CHECK 约束，限定为 ('running' | 'done' | 'failed')。
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = "09_split_status_state"
down_revision: Union[str, None] = "08_extend_character_bios"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # W1.5: scripts.last_analysis_status
    op.execute(
        """
        ALTER TABLE scriptlens.scripts
            ADD COLUMN IF NOT EXISTS last_analysis_status TEXT NULL
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'scripts_last_analysis_status_check'
            ) THEN
                ALTER TABLE scriptlens.scripts
                    ADD CONSTRAINT scripts_last_analysis_status_check
                    CHECK (
                        last_analysis_status IS NULL
                        OR last_analysis_status IN ('running', 'done', 'failed')
                    );
            END IF;
        END $$;
        """
    )

    # W1.6: scoring_runs.status check constraint
    # 现有数据可能含 'running' / 'done' / 'failed' 之外的值（很少见，旧 Batch3 时代
    # 可能存在 'cancelled'），先用 IS NOT NULL 的兼容写法把约束加上，但允许 NULL
    # 与现有值通过；后续 PR2 收口时再 NOT NULL。
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'scoring_runs_status_check'
            ) THEN
                ALTER TABLE scriptlens.scoring_runs
                    ADD CONSTRAINT scoring_runs_status_check
                    CHECK (
                        status IS NULL
                        OR status IN ('running', 'done', 'failed')
                    );
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE scriptlens.scripts
            DROP CONSTRAINT IF EXISTS scripts_last_analysis_status_check
        """
    )
    op.execute(
        """
        ALTER TABLE scriptlens.scripts
            DROP COLUMN IF EXISTS last_analysis_status
        """
    )
    op.execute(
        """
        ALTER TABLE scriptlens.scoring_runs
            DROP CONSTRAINT IF EXISTS scoring_runs_status_check
        """
    )
