"""Wave C-rewrite C1c: BEFORE UPDATE trigger to invalidate brief_json on text change.

Revision ID: 13_brief_invalidate_trigger
Revises: 12_add_scenes_brief_json
Create Date: 2026-06-02

Background
==========
``scriptlens.scenes.brief_json`` 是 plan/execute 链路用的预消化简介（v4 C1c）。
原文改了，brief 必须立刻失效，否则下游 LLM 会看到「旧场景的简介 + 新场景的
text」这种自相矛盾的输入。

可选实现路径有三种：

1. 在每个 UPDATE scenes.text 的调用点手动 `UPDATE ... SET brief_json = NULL`
   —— 容易漏（写场原文的地方有 5 处：service/script_query_service / script_vfs /
   agent_runtime/script_tools / router/script_rt 等）
2. 在 ORM 层（drizzle / sqlalchemy）做 hook —— ScriptLens 后端是裸 SQL，没有
   集中的 ORM hook 点
3. **PG trigger**（本 migration 选择）—— 失效逻辑在 DB 层声明，无论从哪个
   client 改 text 都自动失效。零侵入业务代码。

Trigger 设计
============
``BEFORE UPDATE OF text``：只在显式 UPDATE 了 text 列时触发，对其它 UPDATE
（仅改 brief_json / 仅改 characters[] 等）零开销。
``NEW.text IS DISTINCT FROM OLD.text``：NULL-safe 比较，避免「相同 text 的
no-op UPDATE」也触发失效。

Idempotency
===========
upgrade: ``DROP TRIGGER IF EXISTS`` + ``CREATE OR REPLACE FUNCTION`` —— 重复
执行不会炸。
downgrade: 删 trigger + 删 function。无数据损失（brief_json 可以下次 plan
自动再生成）。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "13_brief_invalidate_trigger"
down_revision: Union[str, None] = "12_add_scenes_brief_json"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION scriptlens.scene_brief_invalidate()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.text IS DISTINCT FROM OLD.text THEN
                NEW.brief_json := NULL;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS scenes_brief_invalidate ON scriptlens.scenes")
    op.execute(
        """
        CREATE TRIGGER scenes_brief_invalidate
            BEFORE UPDATE OF text ON scriptlens.scenes
            FOR EACH ROW
            EXECUTE FUNCTION scriptlens.scene_brief_invalidate();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS scenes_brief_invalidate ON scriptlens.scenes")
    op.execute("DROP FUNCTION IF EXISTS scriptlens.scene_brief_invalidate()")
