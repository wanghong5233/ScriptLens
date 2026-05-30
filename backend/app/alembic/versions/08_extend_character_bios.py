"""Extend character_bios with dialogue_style and notable_scenes.

Revision ID: 08_extend_character_bios
Revises: 07_add_character_bios
Create Date: 2026-05-30

补齐 v1-mvp 人物小传缺失的两个工业字段：

1. dialogue_style (TEXT)
   对齐 Sudowrite Story Bible "Dialogue Sample / Dialogue Style" 字段：
   该角色"说话风格"的一段话描述（节奏 / 语气 / 口头禅模式 / 用词偏好）。
   下游剧本续写 / 角色对白生成会作为 style anchor 注入 prompt，
   仅靠 catchphrases 列原文台词不足以表达"风格指令"，故独立成段。

2. notable_scenes (JSONB)
   形如 [{"scene_id": "<uuid>", "behavior": "<≤120字 该角色在该场做了什么>"}]
   3 条左右。补"原文证据链"：persona_surface / persona_core 是 LLM 总结，
   缺乏可追溯性；notable_scenes 把"该角色最具代表性的 3 场行为"+ scene_id
   一起存下来，前端可"点击 → 跳转原文"，编剧能直接看到结论从哪里来。

字段独立成 column 而非塞进 evidence JSONB：前端 Tab 渲染需要稳定 schema，
evidence 是 debug 字段（model/elapsed_ms 等），混在一起会让前端取数路径
分裂。落库时 dialogue_style 默认空字符串、notable_scenes 默认空数组，
保持向前兼容（已有 bios 行不需要回填）。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "08_extend_character_bios"
down_revision: Union[str, None] = "07_add_character_bios"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE scriptlens.character_bios
            ADD COLUMN IF NOT EXISTS dialogue_style  TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS notable_scenes  JSONB NOT NULL DEFAULT '[]'::jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE scriptlens.character_bios
            DROP COLUMN IF EXISTS dialogue_style,
            DROP COLUMN IF EXISTS notable_scenes
        """
    )
