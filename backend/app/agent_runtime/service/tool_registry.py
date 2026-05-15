"""ScriptLens Agent 工具注册表。"""

import logging

from .tools import ToolRegistry
from .tools.response_tools import ReplyToUserTool
from .tools.script_tools import (
    ExtractCharactersTool,
    LocateScenesTool,
    PropDimensionRewriteTool,
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
    # 兼容旧工具名：内部会转发到改写三件套。
    registry.register(PropDimensionRewriteTool())
    registry.register(ProposeRewriteTool())

    registry.register(WebSearchTool())

    registry.register(ReplyToUserTool())

    logger.info("ScriptLens tool registry initialized: %s tools", len(registry.list_tools()))
    return registry
