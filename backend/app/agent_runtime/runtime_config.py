"""Agent runtime 配置加载器（YAML → Pydantic → 缓存 effective dict）。

输入文件：``configs/agent_runtime.yaml``
输出 API：``get_runtime_profile(intent: str | None) -> RuntimeProfile``

设计要点：

1. **三 section**（``tool_budgets`` / ``loop_limits`` / ``tool_whitelist``）统一在
   per-intent 维度治理；intent 缺省的键沿用 default。
2. **fail-fast**：YAML 语法错、Pydantic 校验错、工具名未注册（这里只校验类型，
   工具名归属 tool_registry 校验）都立即抛错；不静默回退硬编码。
3. **env 覆盖**：``AGENT_RUNTIME__<INTENT>__<SECTION>__<KEY>=<value>``
   例：``AGENT_RUNTIME__EDIT__TOOL_BUDGETS__REWRITE_SCENE_TOOL=20``
   ``AGENT_RUNTIME__DEFAULT__LOOP_LIMITS__LLM_RETRY_ATTEMPTS=5``
   tool_whitelist 通过 env 覆盖时用逗号分隔，如
   ``AGENT_RUNTIME__QA__TOOL_WHITELIST=read_scene_tool,reply_to_user_tool``
4. **mtime-aware 缓存**：进程内只读一次；改 yaml 后调
   :func:`invalidate_runtime_config_cache` 或重启进程。

为什么不复用 ``ConfigLoader``：那个是 JSON 专用，且 fallback 行为是错误时返回
空 dict（demo 时代设计）。本模块要的是 fail-fast + Pydantic 强类型 + env 覆盖。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


# ============================================================
# Pydantic schema —— 给 yaml 文件做强类型校验
# ============================================================


class _LoopLimitsModel(BaseModel):
    """ReAct 主循环 guardrail 阈值。

    全部字段可选：intent override 里通常只覆盖几个键，未声明的合并时沿用 default。
    """

    model_config = ConfigDict(extra="forbid")

    recovery_actions_max: Optional[int] = Field(default=None, ge=0)
    consecutive_tool_failures_threshold: Optional[int] = Field(default=None, ge=1)
    same_tool_convergence_count: Optional[int] = Field(default=None, ge=2)
    llm_retry_attempts: Optional[int] = Field(default=None, ge=1)
    llm_retry_backoff_base_seconds: Optional[float] = Field(default=None, gt=0.0)
    llm_retry_backoff_factor: Optional[float] = Field(default=None, ge=1.0)
    guardrail_reply_max_tokens: Optional[int] = Field(default=None, ge=64)


class _ProfileModel(BaseModel):
    """单个 profile（default 或某个 intent）的原始 YAML 块。"""

    model_config = ConfigDict(extra="forbid")

    tool_budgets: Dict[str, int] = Field(default_factory=dict)
    loop_limits: _LoopLimitsModel = Field(default_factory=_LoopLimitsModel)
    # null → 不限制；空列表 → 一个工具都不暴露（除非外层强制带 reply_to_user_tool）
    tool_whitelist: Optional[List[str]] = None

    @field_validator("tool_budgets")
    @classmethod
    def _validate_tool_budgets(cls, value: Dict[str, int]) -> Dict[str, int]:
        for tool_name, limit in value.items():
            if not isinstance(tool_name, str) or not tool_name:
                raise ValueError(f"tool_budgets key must be non-empty string, got {tool_name!r}")
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise ValueError(f"tool_budgets[{tool_name}] must be int, got {type(limit).__name__}")
            if limit < 0:
                raise ValueError(f"tool_budgets[{tool_name}] must be >= 0, got {limit}")
        return value


# 5 个合法 intent（与 IntentType 同源；unknown 不在配置里，回 default）
_ALLOWED_INTENTS = ("qa", "suggest", "edit", "citation", "file_op")


class _RuntimeConfigModel(BaseModel):
    """``agent_runtime.yaml`` 顶层结构。"""

    model_config = ConfigDict(extra="forbid")

    default: _ProfileModel
    intents: Dict[str, _ProfileModel] = Field(default_factory=dict)

    @field_validator("intents")
    @classmethod
    def _validate_intent_keys(cls, value: Dict[str, _ProfileModel]) -> Dict[str, _ProfileModel]:
        for key in value:
            if key not in _ALLOWED_INTENTS:
                raise ValueError(
                    f"intents.{key!r} 不在允许列表 {_ALLOWED_INTENTS!r} 中；"
                    "请使用 IntentType 枚举值"
                )
        return value


# ============================================================
# 对外的 runtime API：合并后的 effective profile
# ============================================================


@dataclass(frozen=True)
class LoopLimits:
    """ReAct 主循环 6 个 guardrail 阈值（effective 值，所有字段非 None）。"""

    recovery_actions_max: int
    consecutive_tool_failures_threshold: int
    same_tool_convergence_count: int
    llm_retry_attempts: int
    llm_retry_backoff_base_seconds: float
    llm_retry_backoff_factor: float
    guardrail_reply_max_tokens: int


@dataclass(frozen=True)
class RuntimeProfile:
    """单个 intent 的 effective 运行时画像（default 已合并）。"""

    intent: str
    tool_budgets: Dict[str, int]
    loop_limits: LoopLimits
    tool_whitelist: Optional[Tuple[str, ...]] = None  # None = 全开

    def is_tool_allowed(self, tool_name: str) -> bool:
        """白名单未启用时全 True；启用时按 set 查找。"""
        if self.tool_whitelist is None:
            return True
        return tool_name in self.tool_whitelist


# ============================================================
# 加载 + 缓存
# ============================================================


_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "configs" / "agent_runtime.yaml"

_lock = threading.Lock()
_cached_raw: Optional[_RuntimeConfigModel] = None
_cached_mtime: Optional[float] = None
_cached_path: Optional[Path] = None


class AgentRuntimeConfigError(RuntimeError):
    """agent_runtime.yaml 加载或校验失败。"""


def _load_raw_config(path: Path) -> _RuntimeConfigModel:
    """读 yaml + Pydantic 校验。任何错误都抛 ``AgentRuntimeConfigError``。"""
    if not path.exists():
        raise AgentRuntimeConfigError(f"agent runtime config file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fp:
            payload = yaml.safe_load(fp)
    except yaml.YAMLError as exc:
        raise AgentRuntimeConfigError(f"agent runtime config YAML parse failed: {path}") from exc

    if not isinstance(payload, dict):
        raise AgentRuntimeConfigError(
            f"agent runtime config root must be a mapping, got {type(payload).__name__}: {path}"
        )

    try:
        return _RuntimeConfigModel.model_validate(payload)
    except ValidationError as exc:
        raise AgentRuntimeConfigError(
            f"agent runtime config schema validation failed: {path}\n{exc}"
        ) from exc


def _get_cached_config(path: Optional[Path] = None) -> _RuntimeConfigModel:
    """带 mtime 失效的进程级缓存。"""
    global _cached_raw, _cached_mtime, _cached_path
    target = path or _DEFAULT_CONFIG_PATH
    mtime = target.stat().st_mtime if target.exists() else None

    with _lock:
        if (
            _cached_raw is not None
            and _cached_path == target
            and _cached_mtime == mtime
        ):
            return _cached_raw
        config = _load_raw_config(target)
        _cached_raw = config
        _cached_mtime = mtime
        _cached_path = target
        logger.info(
            "agent_runtime config loaded: %s (intents=%s)",
            target,
            sorted(config.intents.keys()),
        )
        return config


def invalidate_runtime_config_cache() -> None:
    """供测试 / 热更场景手动 invalidate。"""
    global _cached_raw, _cached_mtime, _cached_path
    with _lock:
        _cached_raw = None
        _cached_mtime = None
        _cached_path = None


# ============================================================
# env 覆盖解析
# ============================================================


def _parse_env_overrides() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """从环境变量提取覆盖项。

    格式：``AGENT_RUNTIME__<INTENT>__<SECTION>__<KEY>=<value>``

    返回三层嵌套 dict：``overrides[intent][section][key] = value``。intent 用
    小写、section 用小写、key 用小写后传出。无效格式（少于 4 段、section 不识别）
    会 log warning 并跳过——env 错配置不应让 agent 完全无法启动。
    """
    prefix = "AGENT_RUNTIME__"
    valid_sections = {"tool_budgets", "loop_limits", "tool_whitelist"}
    overrides: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for env_name, raw_value in os.environ.items():
        if not env_name.startswith(prefix):
            continue
        parts = env_name[len(prefix):].split("__")
        # tool_whitelist 是"无 key 的整段替换"，格式 PREFIX__INTENT__TOOL_WHITELIST 只有 2 段；
        # 其他 section 必须给 key，至少 3 段。
        if len(parts) < 2:
            logger.warning("env override skipped (too few segments): %s", env_name)
            continue
        intent_part = parts[0].lower()
        section_part = parts[1].lower()

        if intent_part != "default" and intent_part not in _ALLOWED_INTENTS:
            logger.warning("env override unknown intent %s in %s", intent_part, env_name)
            continue
        if section_part not in valid_sections:
            logger.warning("env override unknown section %s in %s", section_part, env_name)
            continue

        if section_part == "tool_whitelist":
            if len(parts) > 2:
                logger.warning(
                    "tool_whitelist env override is whole-section replace; ignored sub-key in %s",
                    env_name,
                )
            value: Any = [t.strip() for t in raw_value.split(",") if t.strip()] or None
            overrides.setdefault(intent_part, {}).setdefault(section_part, {})["__list__"] = value
            continue

        if len(parts) < 3:
            logger.warning(
                "env override skipped (need INTENT__SECTION__KEY for %s): %s",
                section_part,
                env_name,
            )
            continue
        key_part = "__".join(parts[2:]).lower()

        if section_part == "loop_limits":
            try:
                if key_part in ("llm_retry_backoff_base_seconds", "llm_retry_backoff_factor"):
                    value = float(raw_value)
                else:
                    value = int(raw_value)
            except ValueError:
                logger.warning("env override invalid numeric for %s=%r", env_name, raw_value)
                continue
        else:  # tool_budgets
            try:
                value = int(raw_value)
            except ValueError:
                logger.warning("env override invalid int for %s=%r", env_name, raw_value)
                continue

        overrides.setdefault(intent_part, {}).setdefault(section_part, {})[key_part] = value

    return overrides


# ============================================================
# 合并语义：default → intent override → env override
# ============================================================


def _merge_profile(
    intent: str,
    default: _ProfileModel,
    intent_override: Optional[_ProfileModel],
    env_overrides: Dict[str, Dict[str, Any]],
) -> RuntimeProfile:
    # tool_budgets：部分键覆盖
    tool_budgets: Dict[str, int] = dict(default.tool_budgets)
    if intent_override is not None:
        tool_budgets.update(intent_override.tool_budgets)
    for key, val in (env_overrides.get("tool_budgets") or {}).items():
        tool_budgets[key] = int(val)

    # loop_limits：default 字段必须全部就位（否则 yaml 写得不完整）
    base_limits = default.loop_limits.model_dump(exclude_none=False)
    missing = [k for k, v in base_limits.items() if v is None]
    if missing:
        raise AgentRuntimeConfigError(
            f"default.loop_limits 缺少字段：{missing}（必须全部声明，intent 才能做部分覆盖）"
        )
    if intent_override is not None:
        for key, val in intent_override.loop_limits.model_dump(exclude_none=True).items():
            base_limits[key] = val
    for key, val in (env_overrides.get("loop_limits") or {}).items():
        base_limits[key] = val

    loop_limits = LoopLimits(**base_limits)

    # tool_whitelist：intent 整段替换，env 再覆盖整段
    whitelist: Optional[List[str]]
    if intent_override is not None and intent_override.tool_whitelist is not None:
        whitelist = list(intent_override.tool_whitelist)
    else:
        whitelist = list(default.tool_whitelist) if default.tool_whitelist is not None else None

    env_whitelist_slot = env_overrides.get("tool_whitelist")
    if env_whitelist_slot is not None and "__list__" in env_whitelist_slot:
        whitelist = env_whitelist_slot["__list__"]

    return RuntimeProfile(
        intent=intent,
        tool_budgets=tool_budgets,
        loop_limits=loop_limits,
        tool_whitelist=tuple(whitelist) if whitelist is not None else None,
    )


def get_runtime_profile(intent: Optional[str], *, config_path: Optional[Path] = None) -> RuntimeProfile:
    """返回某 intent 的 effective runtime profile。

    Args:
        intent: ``qa`` / ``suggest`` / ``edit`` / ``citation`` / ``file_op`` 或 None / "unknown"
            （回 default）。
        config_path: 仅测试用；正常运行不传，使用默认路径。

    Raises:
        AgentRuntimeConfigError: yaml 缺失、解析失败或 schema 不合规。
    """
    config = _get_cached_config(config_path)
    env_overrides = _parse_env_overrides()

    intent_key = (intent or "").strip().lower()
    intent_override = None
    if intent_key in _ALLOWED_INTENTS:
        intent_override = config.intents.get(intent_key)
    else:
        # unknown / None → 仍带 intent="default" 让调用方知道走兜底
        intent_key = "default"

    layered_env = env_overrides.get(intent_key, {})
    # default 层 env 也合并进去（intent 没显式覆盖时让 env 默认生效）
    if intent_key != "default":
        default_env = env_overrides.get("default", {})
        # 部分键合并：先 default env 再 intent env
        layered_env = _merge_env_layers(default_env, layered_env)

    return _merge_profile(
        intent=intent_key,
        default=config.default,
        intent_override=intent_override,
        env_overrides=layered_env,
    )


def _merge_env_layers(
    lower: Dict[str, Dict[str, Any]], upper: Dict[str, Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for section in {*lower.keys(), *upper.keys()}:
        slot: Dict[str, Any] = {}
        slot.update(lower.get(section) or {})
        slot.update(upper.get(section) or {})
        merged[section] = slot
    return merged
