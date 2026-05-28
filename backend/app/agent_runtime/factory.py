"""ScriptLens Agent 工厂。

复用 doc_studio 的 LaTeXEditAgent / LLMClient / ToolRegistry，但：

1. 短剧没有"工作区文件树"——剧本场景全部在 DB 的 `scriptlens.scenes` 表。
   因此 `_load_workspace_context` 重写为**直接查 DB**，不读 fs：
   - 查 `scriptlens.scripts` 拿剧本元数据（title / status / total_*）
   - 校验 `user_id` 归属（fail aloud：剧本不存在 → raise ScriptNotFoundError；
     越权 → raise ScriptPermissionError；status != 'ready' → raise ScriptNotReadyError）
   - 把元数据塞进 `state.workspace_config`，4 个剧本工具通过 `_resolve_script_id`
     直接拿 `script_id` 用
2. 重写 `tool_call_limits` 为短剧工具预算（doc_studio 默认包含一堆我们
   不注册的 LaTeX/SearchPapers 工具名，留着无害但匹配不到）

LLMClient / ToolRegistry 是无状态可复用的，做成模块级单例。
LaTeXEditAgent 实例每次 chat 都新建（持有 per-request state）。

不变式：本模块**不做任何 try/except 静默吞错**。所有异常按 fail aloud 原则
向上抛，让 router 层映射到具体 HTTP 状态码（404 / 403 / 409）。
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

from utils.database import engine as db_engine

from .service.agent_service import AgentState, LaTeXEditAgent
from .service.llm_client import LLMClient
from .service.tool_registry import create_tool_registry
from .service.tools import ToolRegistry

logger = logging.getLogger(__name__)


_llm_client: Optional[LLMClient] = None
_tool_registry: Optional[ToolRegistry] = None


# ============================================================
# fail-aloud 异常类型（router 层会映射到 HTTP 404 / 403 / 409）
# ============================================================


class ScriptWorkspaceError(Exception):
    """ScriptLens workspace 加载失败的基类（fail aloud）。"""


class ScriptNotFoundError(ScriptWorkspaceError):
    """剧本 id 不存在 → 404。"""


class ScriptPermissionError(ScriptWorkspaceError):
    """剧本归属于其他用户 → 403。"""


class ScriptNotReadyError(ScriptWorkspaceError):
    """剧本 status != 'ready' → 409（解析中 / 失败）。"""


# ============================================================
# 单例 + Agent 工厂
# ============================================================


def get_llm_client() -> LLMClient:
    """LLMClient 单例（OpenAI / DashScope，由 settings 决定）。"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
        logger.info("agent_runtime LLMClient ready: provider=%s model=%s",
                    _llm_client.provider, getattr(_llm_client, "model", None))
    return _llm_client


def get_tool_registry() -> ToolRegistry:
    """ToolRegistry 单例（短剧工具集）。"""
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = create_tool_registry()
        names = [t.get("name") if isinstance(t, dict) else getattr(t, "name", "?")
                 for t in _tool_registry.list_tools()]
        logger.info("agent_runtime tool registry: %s", names)
    return _tool_registry


class ScriptChatAgent(LaTeXEditAgent):
    """ScriptLens 短剧 Chat Agent。

    跟 LaTeXEditAgent 的差异：
    - workspace 概念不同：ScriptLens 的"工作区 = 剧本"在 DB 表里，不在 fs
      → 重写 `_load_workspace_context` 直接查 DB；缺数据 / 越权 / 未就绪
        全部 raise，不做静默降级
    - 把剧本元数据（id / title / status / total_episodes / total_scenes）
      注入 state.workspace_config，4 个剧本工具用 `_resolve_script_id` 取 script_id
    - tool_call_limits 改为短剧工具预算
    """

    def __init__(self, llm_client: LLMClient, tool_registry: ToolRegistry, *, script_id: str) -> None:
        super().__init__(llm_client=llm_client, tool_registry=tool_registry)
        self._script_id = script_id
        self.tool_call_limits = {
            "score_dimension_tool": 6,            # 六维 + compliance；rescore 闭环至少跑 1 次
            "locate_scenes_tool": 6,              # 多场景定位允许多次但有上限
            "extract_characters_tool": 1,         # 全剧人物 1 次足矣
            "read_scene_tool": 8,                 # 单会话多次读取局部场景
            "rewrite_selection_scene_tool": 8,    # 选区级改写（翻译/润色/局部重写）
            "propose_full_script_plan_tool": 2,   # 计划生成 + 一次兜底
            "rewrite_scene_tool": 12,             # execute 阶段逐场改写（默认上限 12 场）
            "propose_rewrite_tool": 3,            # 单场临时改写兜底入口（chat 自然指令）
            "propose_dimension_rewrite_tool": 2,  # 兼容旧入口：内部转发到三件套
            "web_search_tool": 3,                 # reuse-matrix §5.1 规定上限
            "reply_to_user_tool": 1,              # ReAct 强制收尾
        }

    async def _load_workspace_context(
        self, state: AgentState, include_edit_state: bool = True
    ) -> None:
        """从 DB 加载剧本元数据；fail aloud。

        覆盖 LaTeXEditAgent 的文件系统扫描逻辑——ScriptLens 的"工作区"是
        DB 里的一行 `scriptlens.scripts` 记录，不是 fs 目录。任何异常都向上抛，
        让 router 层映射到具体 HTTP code。
        """
        with db_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id::text AS id, title, source_format, status,
                           total_episodes, total_scenes, total_chars,
                           user_id, failure_reason
                    FROM scriptlens.scripts
                    WHERE id = :sid
                    """
                ),
                {"sid": self._script_id},
            ).mappings().first()

        if row is None:
            raise ScriptNotFoundError(f"剧本 {self._script_id} 不存在")

        if int(row["user_id"]) != int(state.user_id):
            raise ScriptPermissionError(
                f"用户 user_id={state.user_id} 无权访问剧本 {self._script_id}"
            )

        if row["status"] != "ready":
            raise ScriptNotReadyError(
                f"剧本 {self._script_id} 当前 status={row['status']}"
                + (f"（failure_reason={row['failure_reason']}）" if row["failure_reason"] else "")
                + "；需 status=ready 才能 chat"
            )

        # ScriptLens 不是文件工作区：files / citation / original_files 永远空
        state.workspace_files = []
        state.citation_mappings = {}
        state.original_file_contents = {}
        state.workspace_config = {
            "script_id": self._script_id,
            "script_title": row["title"],
            "script_source_format": row["source_format"],
            "script_status": row["status"],
            "script_total_episodes": row["total_episodes"],
            "script_total_scenes": row["total_scenes"],
            "script_total_chars": row["total_chars"],
        }
        logger.info(
            "ScriptChatAgent workspace loaded: script_id=%s title=%r episodes=%s scenes=%s",
            self._script_id, row["title"], row["total_episodes"], row["total_scenes"],
        )


def build_chat_agent(script_id: str) -> ScriptChatAgent:
    """构造 per-request ScriptChatAgent。"""
    return ScriptChatAgent(
        llm_client=get_llm_client(),
        tool_registry=get_tool_registry(),
        script_id=script_id,
    )
