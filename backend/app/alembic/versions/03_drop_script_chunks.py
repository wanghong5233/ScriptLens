"""ScriptLens drop script_chunks table

Revision ID: 03_drop_script_chunks
Revises: 02_add_script_operations
Create Date: 2026-05-06

拆除 embedding 路径——`scriptlens.script_chunks`（每场一份 1024 维 pgvector
向量）整表删除。

为什么删（详见 `docs/04-script-pipeline.md` §4.4）：
- 评分链路：按维度读取 scenes.text，不依赖向量检索
- 证据（extract_quote）：LLM 输出 scene_no 反查，不查向量
- 任务派发（<TASK_META>）：已携带 scene_id，不查向量
- Agent locate_scenes_tool：BM25（一级）+ LLM metadata（二级兜底）已足够，
  且长剧场景下 embedding 反而是 ingestion 瓶颈（1500 次 DashScope API call）

`scenes.text` 的 BM25 索引（`idx_scenes_text_fts`，建于 01_init_scriptlens）
保留——这是 retrieve_scenes 的一级检索路径。

向后兼容：scenes 表保留 start_line / end_line（原始 paragraphs 数组下标，
未来溯源原文用）。

downgrade：恢复 script_chunks 表结构（不重建向量数据，需要重跑 ingestion 才有）。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "03_drop_script_chunks"
down_revision: Union[str, None] = "02_add_script_operations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 索引随表自动 DROP，无需显式 drop index
    op.execute("DROP TABLE IF EXISTS scriptlens.script_chunks")


def downgrade() -> None:
    """复原表结构（向量数据需重跑 ingestion 才有）。"""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.script_chunks (
            id          UUID PRIMARY KEY,
            scene_id    UUID NOT NULL REFERENCES scriptlens.scenes(id) ON DELETE CASCADE,
            script_id   UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            text        TEXT NOT NULL,
            embedding   vector(1024),
            metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_script_chunks_script "
        "ON scriptlens.script_chunks (script_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_script_chunks_text_fts "
        "ON scriptlens.script_chunks USING gin (to_tsvector('simple', coalesce(text, '')))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_script_chunks_embedding_ivfflat "
        "ON scriptlens.script_chunks USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100) "
        "WHERE embedding IS NOT NULL"
    )
