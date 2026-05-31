"""agent_runtime.yaml + runtime_config loader 烟雾测试。

覆盖：
- 默认 yaml 能正常加载，Pydantic 校验通过
- default profile 7 个 loop_limits 字段全部就位（None 不允许）
- intent override 部分键合并（tool_budgets 合并，loop_limits 部分覆盖）
- intent.tool_whitelist 整段替换 default.tool_whitelist
- unknown intent / None 回 default
- env 覆盖优先级：default env < intent env
- 非法 intent key（yaml 里写错）抛 AgentRuntimeConfigError
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml

from agent_runtime.runtime_config import (
    AgentRuntimeConfigError,
    LoopLimits,
    RuntimeProfile,
    get_runtime_profile,
    invalidate_runtime_config_cache,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """每个 case 都跑在干净缓存上,避免 env 串扰。"""
    invalidate_runtime_config_cache()
    # 清理可能残留的 env 覆盖
    for key in list(os.environ.keys()):
        if key.startswith("AGENT_RUNTIME__"):
            del os.environ[key]
    yield
    invalidate_runtime_config_cache()
    for key in list(os.environ.keys()):
        if key.startswith("AGENT_RUNTIME__"):
            del os.environ[key]


# ============================================================
# 默认 yaml 加载
# ============================================================


def test_default_profile_loads_with_all_loop_limit_fields() -> None:
    """default profile 必须给齐 7 个 loop_limits 字段（合并时不允许 None 残留）。"""
    profile = get_runtime_profile(None)
    assert profile.intent == "default"
    assert isinstance(profile.loop_limits, LoopLimits)
    # 7 个字段均为正常值
    assert profile.loop_limits.recovery_actions_max >= 0
    assert profile.loop_limits.consecutive_tool_failures_threshold >= 1
    assert profile.loop_limits.same_tool_convergence_count >= 2
    assert profile.loop_limits.llm_retry_attempts >= 1
    assert profile.loop_limits.llm_retry_backoff_base_seconds > 0
    assert profile.loop_limits.llm_retry_backoff_factor >= 1.0
    assert profile.loop_limits.guardrail_reply_max_tokens >= 64

    # 短剧的 11 个工具均有 budget
    assert "score_dimension_tool" in profile.tool_budgets
    assert "reply_to_user_tool" in profile.tool_budgets

    # default 未设白名单 → None
    assert profile.tool_whitelist is None


# ============================================================
# intent 覆盖 default：部分键 / 整段
# ============================================================


def test_qa_intent_partial_override_merges_tool_budgets() -> None:
    """qa intent 只覆盖 web_search_tool=5；其他 budget 沿用 default。"""
    qa = get_runtime_profile("qa")
    assert qa.intent == "qa"

    default = get_runtime_profile(None)
    # qa 显式覆盖了 web_search_tool
    assert qa.tool_budgets["web_search_tool"] == 5
    # qa 没声明的键沿用 default
    assert qa.tool_budgets["score_dimension_tool"] == default.tool_budgets["score_dimension_tool"]


def test_qa_intent_replaces_tool_whitelist() -> None:
    """qa.tool_whitelist 是显式列表 → 必须替换 default.None，不允许合并。"""
    qa = get_runtime_profile("qa")
    assert qa.tool_whitelist is not None
    assert "reply_to_user_tool" in qa.tool_whitelist
    # web_search 在 qa 白名单里
    assert "web_search_tool" in qa.tool_whitelist
    # rewrite_scene_tool 在 qa 白名单里看不到（qa 不做改写）
    assert "rewrite_scene_tool" not in qa.tool_whitelist


def test_citation_intent_loop_limits_partial_override() -> None:
    """citation 显式把 llm_retry_attempts 改为 2；其余 loop_limits 沿用 default。"""
    citation = get_runtime_profile("citation")
    default = get_runtime_profile(None)
    assert citation.loop_limits.llm_retry_attempts == 2
    # backoff_base 没覆盖,沿用 default
    assert (
        citation.loop_limits.llm_retry_backoff_base_seconds
        == default.loop_limits.llm_retry_backoff_base_seconds
    )


# ============================================================
# 兜底
# ============================================================


def test_unknown_intent_falls_back_to_default() -> None:
    """yaml 没声明的 intent（如 unknown / 任意字符串）→ default profile。"""
    profile = get_runtime_profile("unknown")
    default = get_runtime_profile(None)
    assert profile.intent == "default"
    assert profile.loop_limits == default.loop_limits
    assert profile.tool_whitelist == default.tool_whitelist


def test_none_intent_returns_default_profile() -> None:
    profile = get_runtime_profile(None)
    assert profile.intent == "default"


# ============================================================
# env 覆盖
# ============================================================


def test_env_override_default_loop_limits_takes_effect_across_intents() -> None:
    """AGENT_RUNTIME__DEFAULT__LOOP_LIMITS__LLM_RETRY_ATTEMPTS=5 → 所有未显式覆盖的 intent 都拿 5。"""
    os.environ["AGENT_RUNTIME__DEFAULT__LOOP_LIMITS__LLM_RETRY_ATTEMPTS"] = "5"
    qa = get_runtime_profile("qa")  # qa 没显式覆盖 llm_retry_attempts
    assert qa.loop_limits.llm_retry_attempts == 5


def test_intent_env_override_beats_default_env_override() -> None:
    """default env vs intent env 同时存在时,intent 优先。"""
    os.environ["AGENT_RUNTIME__DEFAULT__LOOP_LIMITS__LLM_RETRY_ATTEMPTS"] = "7"
    os.environ["AGENT_RUNTIME__EDIT__LOOP_LIMITS__LLM_RETRY_ATTEMPTS"] = "9"
    edit = get_runtime_profile("edit")
    assert edit.loop_limits.llm_retry_attempts == 9
    qa = get_runtime_profile("qa")
    assert qa.loop_limits.llm_retry_attempts == 7  # qa 走 default env


def test_env_override_tool_budget_int() -> None:
    os.environ["AGENT_RUNTIME__EDIT__TOOL_BUDGETS__REWRITE_SCENE_TOOL"] = "20"
    edit = get_runtime_profile("edit")
    assert edit.tool_budgets["rewrite_scene_tool"] == 20


def test_env_override_tool_whitelist_replaces_list() -> None:
    """env tool_whitelist 整段替换。"""
    os.environ["AGENT_RUNTIME__QA__TOOL_WHITELIST"] = "read_scene_tool,reply_to_user_tool"
    qa = get_runtime_profile("qa")
    assert qa.tool_whitelist == ("read_scene_tool", "reply_to_user_tool")


def test_invalid_env_int_is_ignored() -> None:
    """非数字的 env 应被忽略,而不是让加载崩溃。"""
    os.environ["AGENT_RUNTIME__EDIT__TOOL_BUDGETS__REWRITE_SCENE_TOOL"] = "notanint"
    edit = get_runtime_profile("edit")
    # 应该 fallback 到 yaml 里的值（16）
    assert isinstance(edit.tool_budgets["rewrite_scene_tool"], int)


# ============================================================
# 异常路径：fail-fast
# ============================================================


def test_unknown_intent_key_in_yaml_raises(tmp_path: Path) -> None:
    """yaml 写了非法 intent 名 → AgentRuntimeConfigError。"""
    bad_yaml = tmp_path / "agent_runtime.yaml"
    bad_yaml.write_text(
        textwrap.dedent(
            """
            default:
              tool_budgets: {reply_to_user_tool: 1}
              loop_limits:
                recovery_actions_max: 2
                consecutive_tool_failures_threshold: 2
                same_tool_convergence_count: 3
                llm_retry_attempts: 3
                llm_retry_backoff_base_seconds: 0.35
                llm_retry_backoff_factor: 2.0
                guardrail_reply_max_tokens: 520
            intents:
              not_a_valid_intent:
                tool_budgets: {}
            """
        ).strip(),
        encoding="utf-8",
    )
    invalidate_runtime_config_cache()
    with pytest.raises(AgentRuntimeConfigError):
        get_runtime_profile(None, config_path=bad_yaml)


def test_missing_default_loop_limit_field_raises(tmp_path: Path) -> None:
    """default.loop_limits 缺字段（None 残留）→ merge 时抛错。"""
    bad_yaml = tmp_path / "agent_runtime.yaml"
    bad_yaml.write_text(
        textwrap.dedent(
            """
            default:
              tool_budgets: {reply_to_user_tool: 1}
              loop_limits:
                recovery_actions_max: 2
                # 故意漏掉 guardrail_reply_max_tokens 等关键字段
            """
        ).strip(),
        encoding="utf-8",
    )
    invalidate_runtime_config_cache()
    with pytest.raises(AgentRuntimeConfigError):
        get_runtime_profile(None, config_path=bad_yaml)


def test_negative_tool_budget_raises(tmp_path: Path) -> None:
    bad_yaml = tmp_path / "agent_runtime.yaml"
    bad_yaml.write_text(
        textwrap.dedent(
            """
            default:
              tool_budgets: {reply_to_user_tool: -1}
              loop_limits:
                recovery_actions_max: 2
                consecutive_tool_failures_threshold: 2
                same_tool_convergence_count: 3
                llm_retry_attempts: 3
                llm_retry_backoff_base_seconds: 0.35
                llm_retry_backoff_factor: 2.0
                guardrail_reply_max_tokens: 520
            """
        ).strip(),
        encoding="utf-8",
    )
    invalidate_runtime_config_cache()
    with pytest.raises(AgentRuntimeConfigError):
        get_runtime_profile(None, config_path=bad_yaml)


# ============================================================
# RuntimeProfile.is_tool_allowed
# ============================================================


def test_is_tool_allowed_when_whitelist_none() -> None:
    profile = get_runtime_profile(None)
    assert profile.tool_whitelist is None
    assert profile.is_tool_allowed("any_tool_name")  # 全开


def test_is_tool_allowed_filters_on_whitelist() -> None:
    qa = get_runtime_profile("qa")
    assert qa.is_tool_allowed("reply_to_user_tool")
    assert not qa.is_tool_allowed("rewrite_scene_tool")
