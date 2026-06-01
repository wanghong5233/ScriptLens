"""Wave C-rewrite: add scriptlens.scenes.brief_json for plan/execute prompts.

Revision ID: 12_add_scenes_brief_json
Revises: 11_drop_v3_scoring_tables
Create Date: 2026-06-02

Background
==========
rewrite_chain.propose_plan / execute_plan_step 当前喂给 LLM 的场次清单是 110 字
的纯文本省略号末截，外加 ``scenes.characters`` 顿号拼接的角色名数组。LLM 根本
看不到「这场冲突是什么 / 主角做了什么 / 哪些角色是无台词工具人」，导致 plan 阶段
经常输出机械化模板话术（典型例：producibility 维度无差别要求“减少主角同框互动”，
等于把男一女一的主线对手戏切掉）。

业界惯例（参考 OpenAI Cookbook / DSPy / LangChain LLM-as-judge 链路）是：评分阶段
顺手把每场结构化简介落库，下游所有 plan / execute / critic 链路读这同一份 brief，
而不是每次再让 LLM 看 110 字裸文本现猜。

Schema decision
===============
新增 ``scriptlens.scenes.brief_json JSONB DEFAULT NULL``。具体字段约定在
``service.script_tools.scene_brief.SceneBrief`` pydantic 模型里维护，**不**写进
表注释 / 不加 jsonb_path_ops 约束——因为：

1. brief 是缓存而非真值，schema 演进时直接置 NULL 重算，不应被 PG 约束绑死。
2. plan/execute 读取时走 pydantic 校验，schema drift 在应用层即时发现。

写入时机（在后续 commit 落地）：
- scoring 主链跑完后批量补齐
- execute_plan_step 改写某场后 **置 NULL**（让 brief 跟随原文失效）
- plan 链路读到 NULL 时 on-demand 触发生成

Idempotency
===========
upgrade: ``ADD COLUMN IF NOT EXISTS`` —— 重复执行不会炸；新行 default NULL，
完全向后兼容，老代码继续走旧 110 字摘要路径不受影响。
downgrade: ``DROP COLUMN IF EXISTS`` —— 数据丢失不可逆，但 brief 是缓存，
丢了下次 plan 会再生成。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "12_add_scenes_brief_json"
down_revision: Union[str, None] = "11_drop_v3_scoring_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE scriptlens.scenes "
        "ADD COLUMN IF NOT EXISTS brief_json JSONB DEFAULT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE scriptlens.scenes DROP COLUMN IF EXISTS brief_json")
