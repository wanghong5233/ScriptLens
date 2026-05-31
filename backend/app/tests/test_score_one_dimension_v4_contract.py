"""Wave C-2 契约测试：score_one_dimension / agent score_dimension_tool 切到 v4 5 维。

保证：
1. service.script_report_service.score_one_dimension 接受 v4 5 维 + compliance
2. v3 6 维 dim_key 显式 ValueError（不静默映射，避免维度概念错配）
3. agent script_tools._DIMENSIONS / _REWRITE_TARGET_DIMENSIONS 切到 v4 5 维
4. ProposeRewriteTool 不允许 compliance 作为 target_dimension

不 mock 数据库 / LLM —— 只测白名单 + 错误路径，单维分计算交给
test_scoring_dimensions.py 覆盖。
"""

from __future__ import annotations

import asyncio

import pytest

from agent_runtime.service.tools.script_tools import (
    _DIMENSIONS,
    _REWRITE_TARGET_DIMENSIONS,
    ProposeRewriteTool,
    ScoreDimensionTool,
)
from service.script_report_service import _V4_DIMENSIONS, score_one_dimension


# ============================================================
# 1. 白名单契约
# ============================================================

_V4_5DIMS = {"hook", "archetype", "payoff", "monetization", "producibility"}
_V3_6DIMS = {"story", "character", "concept", "emotion", "pacing", "dialogue"}


def test_v4_whitelist_covers_5_dims_plus_compliance():
    """白名单 = v4 5 维 + compliance，缺一不可"""
    assert _V4_DIMENSIONS == _V4_5DIMS | {"compliance"}


def test_v4_whitelist_does_not_include_v3_legacy_dims():
    """v3 6 维不在 v4 白名单内，避免静默路由到错的 dim"""
    assert not (_V4_DIMENSIONS & _V3_6DIMS)


def test_agent_score_dimension_enum_matches_v4_whitelist():
    """ScoreDimensionTool 的 enum 必须与 service 层白名单一致，否则前端 / Agent 会传错"""
    assert set(_DIMENSIONS) == _V4_DIMENSIONS


def test_agent_rewrite_target_excludes_compliance():
    """改写工具不接受 compliance —— 合规问题必须人工二次审核，不能让 LLM 改剧本"""
    assert set(_REWRITE_TARGET_DIMENSIONS) == _V4_5DIMS
    assert "compliance" not in _REWRITE_TARGET_DIMENSIONS


# ============================================================
# 2. v3 dim_key 拒绝（service 层）
# ============================================================


@pytest.mark.parametrize("legacy_dim", sorted(_V3_6DIMS))
def test_score_one_dimension_rejects_v3_dim_key(legacy_dim):
    """v3 6 维 dim_key 必须显式抛 ValueError（不静默映射 → v4），保证升级提示明确"""

    async def _run():
        await score_one_dimension(script_id="any", dimension=legacy_dim)

    with pytest.raises(ValueError) as exc_info:
        asyncio.run(_run())
    assert "v3" in str(exc_info.value) or "v4" in str(exc_info.value)
    assert legacy_dim in str(exc_info.value) or "valid" in str(exc_info.value)


def test_score_one_dimension_rejects_garbage_dim_key():
    """非 v3 / v4 / compliance 的随便字符串也要 ValueError"""

    async def _run():
        await score_one_dimension(script_id="any", dimension="totally_made_up")

    with pytest.raises(ValueError) as exc_info:
        asyncio.run(_run())
    assert "totally_made_up" in str(exc_info.value)


# ============================================================
# 3. Agent tool ValueError 路径
# ============================================================


def test_score_dimension_tool_rejects_v3_dim_key():
    """Agent 工具白名单同样拒绝 v3 dim_key（防止 LLM 学到旧 dim 调错）"""
    tool = ScoreDimensionTool()

    class _FakeState:
        workspace_config = {"script_id": "sid-1"}

    result = asyncio.run(tool.execute(_FakeState(), {"dimension": "story"}))
    assert not result.success
    assert "dimension" in (result.error or "").lower()


def test_propose_rewrite_tool_rejects_compliance():
    """改写工具不接受 compliance（即使它在 ScoreDimensionTool 白名单里）"""
    tool = ProposeRewriteTool()

    class _FakeState:
        workspace_config = {"script_id": "sid-1"}

    result = asyncio.run(
        tool.execute(
            _FakeState(),
            {"scene_id": "scene-1", "target_dimension": "compliance", "issue": "X"},
        )
    )
    assert not result.success
    assert "compliance" in (result.error or "").lower()


def test_propose_rewrite_tool_rejects_v3_dim_key():
    tool = ProposeRewriteTool()

    class _FakeState:
        workspace_config = {"script_id": "sid-1"}

    result = asyncio.run(
        tool.execute(
            _FakeState(),
            {"scene_id": "scene-1", "target_dimension": "story", "issue": "X"},
        )
    )
    assert not result.success
    assert "target_dimension" in (result.error or "").lower()


# ============================================================
# 4. parameters_schema enum 也要切到 v4（防止 LLM 看到 enum 后再选错）
# ============================================================


def test_score_dimension_tool_parameters_schema_enum_is_v4():
    tool = ScoreDimensionTool()
    enum_list = tool.parameters_schema["properties"]["dimension"]["enum"]
    assert set(enum_list) == _V4_DIMENSIONS


def test_propose_rewrite_tool_parameters_schema_enum_is_v4_no_compliance():
    tool = ProposeRewriteTool()
    enum_list = tool.parameters_schema["properties"]["target_dimension"]["enum"]
    assert set(enum_list) == set(_REWRITE_TARGET_DIMENSIONS)
    assert "compliance" not in enum_list
