"""ScriptLens Agent 工具注册表。"""

import logging

from .tools import ToolRegistry
from .tools.response_tools import ReplyToUserTool
from .tools.script_tools import (
    ExtractCharactersTool,
    LocateScenesTool,
    ParallelRewriteScenesTool,
    ProposeFullScriptPlanTool,
    ProposeRewriteTool,
    ReadSceneTool,
    RewriteSelectionSceneTool,
    RewriteSceneTool,
    ScoreDimensionTool,
)
from .tools.web_search_tool import WebSearchTool

logger = logging.getLogger(__name__)


def create_tool_registry() -> ToolRegistry:
    """创建并初始化 ScriptLens Agent 工具注册表。"""
    registry = ToolRegistry()

    registry.register(ScoreDimensionTool())
    registry.register(LocateScenesTool())
    registry.register(ExtractCharactersTool())
    registry.register(ReadSceneTool())
    registry.register(RewriteSelectionSceneTool())
    registry.register(ProposeFullScriptPlanTool())
    registry.register(RewriteSceneTool())
    # 多场场景的批量并发改写。LLM 决策层面一次 tool_call 全派；工具内部
    # asyncio.gather + Semaphore(5) 并发跑 N 路 execute_plan_step（每场独立 prompt）。
    # 决策依据：docs/2026-06-01-parallel-rewrite-scenes-decision.md。
    registry.register(ParallelRewriteScenesTool())
    registry.register(ProposeRewriteTool())

    registry.register(WebSearchTool())

    registry.register(ReplyToUserTool())

    logger.info("ScriptLens tool registry initialized: %s tools", len(registry.list_tools()))
    return registry
