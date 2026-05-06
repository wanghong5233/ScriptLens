"""ScriptLens add script_operations table

Revision ID: 02_add_script_operations
Revises: 01_init_scriptlens
Create Date: 2026-05-05

新增 `scriptlens.script_operations` 表，用于支持 doc-studio timeline 的复用
（M4 改写历史 / 回退预览）。

字段对齐 ScholarMind `DocStudioAPI.OperationSummary` 协议（前端代码不动）：
  - operation_id  -> id (UUID, PK)
  - workspace_id  -> script_id (UUID FK)
  - user_id       -> user_id (INTEGER FK public.users)
  - timestamp     -> created_at (TIMESTAMPTZ)
  - success       -> success (BOOLEAN)
  - intent_type   -> intent_type (TEXT)：rewrite / upload / manual_edit
  - user_intent   -> user_intent (TEXT)：给 timeline 卡片显示的人读描述
  - modified_files-> modified_files (JSONB 数组)：[scene_id, ...]
  - snapshot      -> 拆为 snapshot_before / snapshot_after 两份 JSONB
                     每份结构：{ scene_id: text, ... }
                     这样支持前端 `fetchOperationSnapshotFile(version='before'|'after')`

额外 metadata（非 doc-studio UI 字段）：
  - target_dimension (TEXT)：rewrite 维度（opening_hook 等）
  - rationale       (TEXT)：LLM 解释
  - issue           (TEXT)：用户输入的问题描述
"""

from typing import Sequence, Union

from alembic import op


revision: str = "02_add_script_operations"
down_revision: Union[str, None] = "01_init_scriptlens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE scriptlens.script_operations (
            id                UUID PRIMARY KEY,
            script_id         UUID NOT NULL
                                  REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            user_id           INTEGER NOT NULL
                                  REFERENCES public.users(id) ON DELETE CASCADE,
            intent_type       TEXT NOT NULL,            -- rewrite | upload | manual_edit
            user_intent       TEXT NOT NULL,            -- timeline 卡片描述
            success           BOOLEAN NOT NULL DEFAULT TRUE,
            modified_files    JSONB NOT NULL DEFAULT '[]'::jsonb,
            snapshot_before   JSONB NOT NULL DEFAULT '{}'::jsonb,
            snapshot_after    JSONB NOT NULL DEFAULT '{}'::jsonb,
            target_dimension  TEXT,
            rationale         TEXT,
            issue             TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_script_operations_script_recent "
        "ON scriptlens.script_operations (script_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_script_operations_user "
        "ON scriptlens.script_operations (user_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scriptlens.script_operations")
