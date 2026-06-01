"""Markdown-based prompt loader for the rewrite chain.

设计动机
========
v1-mvp 的 plan/execute prompt 全部嵌在 ``rewrite_chain.py`` 的 f-string 里，
维度专属的方法论散落在 ``_format_output_contract`` 这种几十行函数中，加新维度
或者调整 hook 的判定规则都要改 Python 代码、走 PR、重启服务。这条路在 5 维
都要专属方法论 + few-shot example 的设计目标下走不通。

业界惯例（OpenAI Cookbook / DSPy / LangChain LLM-as-judge 链路）是把 prompt
作为**资源**而非**代码**管理：
- 每个角色（system / output_contract / dimension-specific guidance）独立 md
- Python 只负责 (1) 路径解析 (2) 变量注入 (3) 拼接顺序
- prompt 编辑不需要懂 Python，编辑器/diff/PR review 都能正常处理 md

渲染契约
========
md 文件使用 Python ``str.format`` 占位符语法 ``{var_name}``。
**JSON 字面量必须用 ``{{`` / ``}}`` 转义**，否则 ``str.format`` 会把它当作
未提供的占位符并抛 ``KeyError``。每个 md 顶部的注释块（``<!-- ... -->``）会被
渲染时去掉，方便 prompt 工程师写设计意图但不污染 LLM 上下文。

向后兼容
========
loader 本身不读任何 cn_short_drama.yaml / character_entities —— 调用方负责把
渲染所需的所有上下文以 kwargs 传进来。这让 loader 可以在测试里被 mock，且
prompt 改动不会被无关业务依赖卡住。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


_PROMPTS_ROOT = Path(__file__).parent / "prompts" / "script_studio"
_DESIGN_COMMENT_RE = re.compile(r"<!--.*?-->", flags=re.DOTALL)


class PromptNotFoundError(FileNotFoundError):
    """raise 时即报命中规则失败 — 通常意味着 dimension key 拼错或文件未提交。"""


def _resolve(*relative: str) -> Path:
    """把 ``('plan', 'by_dimension', 'hook.zh.md')`` 解析为绝对路径。

    禁止 ``..`` 跳出 prompts 根目录 — 防御性，避免调用方传非法 dim_key 时
    意外读到 site-packages 任意 md。
    """
    target = (_PROMPTS_ROOT.joinpath(*relative)).resolve()
    try:
        target.relative_to(_PROMPTS_ROOT.resolve())
    except ValueError as exc:
        raise PromptNotFoundError(
            f"prompt path escapes script_studio root: {relative!r}"
        ) from exc
    if not target.is_file():
        raise PromptNotFoundError(f"prompt file not found: {target}")
    return target


@lru_cache(maxsize=64)
def _read_template(path: Path) -> str:
    """读单个 md 模板。

    LRU 缓存避免 plan/execute 高频调用时反复 IO；模板内容是构建期资源，
    服务运行期不应变更，因此缓存安全。重启服务可清。
    """
    raw = path.read_text(encoding="utf-8")
    # 移除 <!-- ... --> 设计注释，避免污染 LLM 上下文
    cleaned = _DESIGN_COMMENT_RE.sub("", raw).strip()
    return cleaned


def load_prompt(*relative: str, **context: Any) -> str:
    """读 md + ``str.format`` 注入 context。

    ``relative`` 是相对 ``prompts/script_studio/`` 的路径分量。
    ``context`` 是模板里 ``{var}`` 占位符的值映射。

    Raises
    ------
    PromptNotFoundError
        模板路径不存在 / 跳出 root。
    KeyError
        模板含 ``{xxx}`` 但 context 没提供 — 调用方有责任传齐参数。
    """
    template = _read_template(_resolve(*relative))
    return template.format(**context)


def load_plan_system(**context: Any) -> str:
    return load_prompt("plan", "_system.zh.md", **context)


def load_plan_output_contract(**context: Any) -> str:
    return load_prompt("plan", "_output_contract.zh.md", **context)


def load_plan_dimension_guidance(dim_key: str, **context: Any) -> str:
    """读单维度的 plan-side 方法论 md。

    dim_key 必须在 ``hook / archetype / payoff / monetization / producibility``
    之内；超出会抛 PromptNotFoundError（_resolve 检测）。

    多维并行（target_dimensions 包含多个）的合并方式由调用方决定 — loader
    一次返回一份。常见做法：plan 阶段建议只按 improvement_brief 的主维度
    加载，避免把 5 份方法论全塞进 prompt 撑爆上下文窗。
    """
    safe_key = dim_key.strip().lower()
    return load_prompt("plan", "by_dimension", f"{safe_key}.zh.md", **context)


def load_execute_system(**context: Any) -> str:
    return load_prompt("execute", "_system.zh.md", **context)


def load_execute_dimension_guidance(dim_key: str, **context: Any) -> str:
    safe_key = dim_key.strip().lower()
    return load_prompt("execute", "by_dimension", f"{safe_key}.zh.md", **context)


def load_plan_critic(**context: Any) -> str:
    return load_prompt("critic", "plan_critic.zh.md", **context)


def load_scene_brief_prompt(**context: Any) -> str:
    """读 scene_brief 生成 prompt（rewrite_chain._ensure_scene_briefs 用）。

    需要的 context 变量见 ``prompts/script_studio/brief/scene_brief.zh.md``：
    episode_no / scene_no / scene_label / characters_block / scene_text。
    """
    return load_prompt("brief", "scene_brief.zh.md", **context)


def known_dimension_keys() -> tuple[str, ...]:
    """返回当前模板系统支持的维度 key 元组（与 plan/by_dimension/*.zh.md 同步）。"""
    return ("hook", "archetype", "payoff", "monetization", "producibility")
