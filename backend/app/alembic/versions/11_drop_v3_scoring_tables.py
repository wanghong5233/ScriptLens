"""Wave C-3c: drop v3 script_scores table.

Revision ID: 11_drop_v3_scoring_tables
Revises: 10_add_scoring_v4_verdict
Create Date: 2026-05-31

Wave C-3c 把 ReportPayload 的 v3 评分字段（decision / overall_score / scorecard /
evaluation）从后端 schema 移除，v4 投资决策评分（service.scoring/）成为唯一评分链路。

数据库侧本次只处理 ``scriptlens.script_scores``：

- ``scriptlens.script_scores``（v3 6 维行表）：Wave C-3a 起停止写入，已无消费者。
  本迁移 DROP 整张表。

保留（暂不动）：

- ``scriptlens.scoring_improvement_actions``：当前 ``service.script_tools.rewrite_chain``
  agent 工具仍在读取。**等 rewrite_chain 完成 v4 五维迁移后**，独立 PR 再 DROP。
- ``scriptlens.scoring_runs.verdict / investment_score``：Wave C-1 已新增，C-3c
  起成为评分主信号源。
- ``scriptlens.reports.decision_payload``：列保留，**内容口径切到 v4 verdict**
  完整 dict（label / overall_score / confidence / dimension_breakdown）。
  不修改列定义，仅业务侧写入逻辑变化（详见 ``service.script_report_service._persist_report``）。

downgrade 重建 ``script_scores`` 为空骨架（不恢复历史数据，仅保证表存在以避免外部脚本崩溃）。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "11_drop_v3_scoring_tables"
down_revision: Union[str, None] = "10_add_scoring_v4_verdict"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scriptlens.script_scores")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.script_scores (
            id                UUID PRIMARY KEY,
            script_id         UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            run_id            UUID REFERENCES scriptlens.scoring_runs(id) ON DELETE SET NULL,
            dimension         TEXT NOT NULL,
            primary_dimension TEXT,
            score             NUMERIC(4, 2),
            tier              TEXT,
            confidence        TEXT,
            coverage_ratio    REAL,
            reason            TEXT,
            signal_refs       JSONB NOT NULL DEFAULT '[]'::jsonb,
            evidence_ref_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_script_scores_run_id "
        "ON scriptlens.script_scores (run_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_script_scores_script "
        "ON scriptlens.script_scores (script_id)"
    )
