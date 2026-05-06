"""ScriptLens Agent 工具注册表。

按 reuse-matrix §5.2 清单注册 6 个工具：
    4 剧本专属：score_dimension / locate_scenes / extract_characters / propose_rewrite
    联网检索：  web_search
    ReAct 终止：reply_to_user

doc_studio 时代的工作区类工具（file_ops / analysis / editing 工作区改写 / SearchPapers）
**不注册**——它们是为 LaTeX 工作区文件树设计的，短剧场景不写 fs。代码保留以备未来扩展。
"""

import logging

from .tools import ToolRegistry
from .tools.response_tools import ReplyToUserTool
from .tools.script_tools import (
    ExtractCharactersTool,
    LocateScenesTool,
    ProposeRewriteTool,
    ScoreDimensionTool,
)
from .tools.web_search_tool import WebSearchTool

logger = logging.getLogger(__name__)


def create_tool_registry() -> ToolRegistry:
    """创建并初始化 ScriptLens Agent 工具注册表（6 个工具）。"""
    registry = ToolRegistry()

    # 4 个剧本专属工具
    registry.register(ScoreDimensionTool())
    registry.register(LocateScenesTool())
    registry.register(ExtractCharactersTool())
    registry.register(ProposeRewriteTool())

    # 联网检索（task §六 真正可工作的 Agent 加分项）
    registry.register(WebSearchTool())

    # ReAct 终止工具（必备）
    registry.register(ReplyToUserTool())

    logger.info("ScriptLens tool registry initialized: %s tools", len(registry.list_tools()))
    return registry
