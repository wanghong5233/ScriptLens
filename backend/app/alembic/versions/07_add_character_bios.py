"""Add character_bios table for v1-mvp persona pipeline.

Revision ID: 07_add_character_bios
Revises: 06_add_scoring_v3
Create Date: 2026-05-30

为 release/v1-mvp 人物小传链路新增独立表 `scriptlens.character_bios`，与
`character_entities` 一对一。设计取舍见 docs/2026-05-29-剧本到分镜-高光集锦
投放-需求与方案.md §5.1（物料层）。

字段语义直接对齐 docs/prompt.jpg 五段式（身份 / 外貌 / 性格 / 经典台词 /
与关键角色关系），其中：

  - identity 拆三段：当前社会身份 / 隐藏身份 / 出身或前世身份；这是短剧
    的高频结构（双重身份/伪装/穿越），合并成一段会让下游 T2I prompt 难以
    取用。
  - appearance 用 JSONB：下游 Seedance / cinegen 注册 character_identity
    时拼 T2I prompt，需要稳定字段（年龄/服装/配饰）；散文形态会让拼装
    脆弱。子字段：age / height / build / facial / signature_props /
    outfit:{material,palette,form}。
  - catchphrases / relations_summary 用 JSONB：每条带 scene_id 回链，
    支持前端"点台词跳到原文"。

不放进 character_entities 而独立成表的理由：character_entities 是
结构化标签（schema 稳定），bios 是 LLM 生成的长文资产（字段会持续
增加，比如下游加 reference_image_url / seedance_character_id），两者
演化频率不同，混在一起会互相牵制。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "07_add_character_bios"
down_revision: Union[str, None] = "06_add_scoring_v3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scriptlens.character_bios (
            id                  UUID PRIMARY KEY,
            script_id           UUID NOT NULL REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
            character_id        UUID NOT NULL REFERENCES scriptlens.character_entities(id) ON DELETE CASCADE,

            identity_present    TEXT NOT NULL DEFAULT '',
            identity_hidden     TEXT NOT NULL DEFAULT '',
            identity_origin     TEXT NOT NULL DEFAULT '',

            appearance          JSONB NOT NULL DEFAULT '{}'::jsonb,

            persona_surface     TEXT NOT NULL DEFAULT '',
            persona_core        TEXT NOT NULL DEFAULT '',
            weakness            TEXT NOT NULL DEFAULT '',
            arc_light           TEXT NOT NULL DEFAULT '',

            catchphrases        JSONB NOT NULL DEFAULT '[]'::jsonb,
            relations_summary   JSONB NOT NULL DEFAULT '[]'::jsonb,

            bio_ver             TEXT NOT NULL DEFAULT 'v1',
            source              TEXT NOT NULL DEFAULT 'llm',
            evidence            JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_character_bios_character "
        "ON scriptlens.character_bios (character_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_character_bios_script "
        "ON scriptlens.character_bios (script_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scriptlens.character_bios")
