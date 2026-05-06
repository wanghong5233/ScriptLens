"""ScriptLens init schema

Revision ID: 01_init_scriptlens
Revises:
Create Date: 2026-05-05

This single migration bootstraps a fresh ScriptLens database:

1. Public schema tables (复用 ScholarMind 既有 ORM 模型) — 创建 8 张表：
   users / sessions / messages / knowledgebases / documents / jobs /
   document_uploads / demo_access_logs

2. ScriptLens 专属 schema `scriptlens` 下 6 张表（PRD §7 + 复用矩阵 §6）：
   scripts / scenes / script_chunks (pgvector) / reports / evidence_refs /
   script_feedback

注意：MVP 不创建 ScholarMind 的 `rag_chunks` 表，因为剧本场景走独立的
`scriptlens.script_chunks`，与 ScholarMind RAG 隔离。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "01_init_scriptlens"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2. 公共 schema 8 张表：用 SQLAlchemy ORM metadata 一次建好
    from models.base import Base
    import models  # noqa: F401  触发所有 ORM 模型注册到 Base.metadata

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    # 3. ScriptLens 独立 schema
    op.execute("CREATE SCHEMA IF NOT EXISTS scriptlens")

    # 3.1 scripts —— 剧本主表
    op.execute(
        """
        CREATE TABLE scriptlens.scripts (
            id              UUID PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            title           TEXT NOT NULL,
            source_format   TEXT NOT NULL,                 -- docx | pdf | txt | md
            raw_storage_path TEXT NOT NULL,                -- /opt/data/scriptlens/storage/...
            total_episodes  INTEGER,
            total_scenes    INTEGER,
            total_chars     INTEGER,
            status          TEXT NOT NULL DEFAULT 'pending', -- pending | parsing | indexing | ready | failed
            failure_reason  TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX idx_scripts_user ON scriptlens.scripts (user_id, created_at DESC)")
    op.execute("CREATE INDEX idx_scripts_status ON scriptlens.scripts (status)")

    # 3.2 scenes —— 场景表（按 segmenter 切分得到）
    op.execute(
        """
        CREATE TABLE scriptlens.scenes (
            id           UUID PRIMARY KEY,
            script_id    UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            episode_no   INTEGER,                          -- 「第 5 集」→ 5
            scene_no     TEXT,                             -- 「5-3」
            scene_label  TEXT,                             -- 「沈宅 夜 内」
            characters   TEXT[] DEFAULT '{}'::TEXT[],
            start_line   INTEGER,
            end_line     INTEGER,
            text         TEXT NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX idx_scenes_script ON scriptlens.scenes (script_id, episode_no, scene_no)")
    op.execute(
        "CREATE INDEX idx_scenes_text_fts "
        "ON scriptlens.scenes USING gin (to_tsvector('simple', coalesce(text, '')))"
    )

    # 3.3 script_chunks —— 向量索引（每场景一个 chunk）
    op.execute(
        """
        CREATE TABLE scriptlens.script_chunks (
            id          UUID PRIMARY KEY,
            scene_id    UUID NOT NULL REFERENCES scriptlens.scenes(id) ON DELETE CASCADE,
            script_id   UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            text        TEXT NOT NULL,
            embedding   vector(1024),                      -- DashScope text-embedding-v3
            metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX idx_script_chunks_script ON scriptlens.script_chunks (script_id)")
    op.execute(
        "CREATE INDEX idx_script_chunks_text_fts "
        "ON scriptlens.script_chunks USING gin (to_tsvector('simple', coalesce(text, '')))"
    )
    op.execute(
        "CREATE INDEX idx_script_chunks_embedding_ivfflat "
        "ON scriptlens.script_chunks USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100) "
        "WHERE embedding IS NOT NULL"
    )

    # 3.4 reports —— 整份分析报告（PRD §7 schema）
    op.execute(
        """
        CREATE TABLE scriptlens.reports (
            id           UUID PRIMARY KEY,
            script_id    UUID NOT NULL UNIQUE REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            report_json  JSONB NOT NULL,                   -- 完整 schema 见 PRD §7
            generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    # 3.5 evidence_refs —— 证据引用（每个评分/决策必须 grounded 到这里）
    op.execute(
        """
        CREATE TABLE scriptlens.evidence_refs (
            id          UUID PRIMARY KEY,
            report_id   UUID NOT NULL REFERENCES scriptlens.reports(id) ON DELETE CASCADE,
            scene_id    UUID NOT NULL REFERENCES scriptlens.scenes(id) ON DELETE CASCADE,
            quote       TEXT NOT NULL,                    -- 原文片段（≤90 字）
            reason      TEXT NOT NULL,                    -- 为何这段支撑该判断
            confidence  TEXT NOT NULL DEFAULT 'medium',   -- high | medium | low
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX idx_evidence_refs_report ON scriptlens.evidence_refs (report_id)")

    # 3.6 script_feedback —— 用户反馈（PRD §10 P3 轻量 skill 机制）
    op.execute(
        """
        CREATE TABLE scriptlens.script_feedback (
            id          UUID PRIMARY KEY,
            script_id   UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            user_id     INTEGER NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            message     TEXT NOT NULL,
            scope       TEXT NOT NULL,                    -- general | dimension | rewrite | scene
            scope_ref   TEXT,                             -- 维度名 / scene_id / rewrite_id
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_script_feedback_script_recent "
        "ON scriptlens.script_feedback (script_id, created_at DESC)"
    )


def downgrade() -> None:
    # 6 张 ScriptLens 表（依赖顺序：先孩子，后父亲）
    op.execute("DROP TABLE IF EXISTS scriptlens.script_feedback")
    op.execute("DROP TABLE IF EXISTS scriptlens.evidence_refs")
    op.execute("DROP TABLE IF EXISTS scriptlens.reports")
    op.execute("DROP TABLE IF EXISTS scriptlens.script_chunks")
    op.execute("DROP TABLE IF EXISTS scriptlens.scenes")
    op.execute("DROP TABLE IF EXISTS scriptlens.scripts")
    op.execute("DROP SCHEMA IF EXISTS scriptlens CASCADE")

    # 公共 schema
    from models.base import Base
    import models  # noqa: F401

    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
