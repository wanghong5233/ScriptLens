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
    PropDimensionRewriteTool,
    ProposeRewriteTool,
    ScoreDimensionTool,
)
from .tools.web_search_tool import WebSearchTool

logger = logging.getLogger(__name__)


def create_tool_registry() -> ToolRegistry:
    """创建并初始化 ScriptLens Agent 工具注册表（7 个工具）。

    剧本专属工具（5 个）：
        - score_dimension_tool / locate_scenes_tool / extract_characters_tool
        - propose_dimension_rewrite_tool ← 主路径：全剧维度改写（plan/execute）
        - propose_rewrite_tool ← 兼容路径：单场改写，chat 自然指令偶尔用
    """
    registry = ToolRegistry()

    registry.register(ScoreDimensionTool())
    registry.register(LocateScenesTool())
    registry.register(ExtractCharactersTool())
    registry.register(PropDimensionRewriteTool())
    registry.register(ProposeRewriteTool())

    registry.register(WebSearchTool())

    registry.register(ReplyToUserTool())

    logger.info("ScriptLens tool registry initialized: %s tools", len(registry.list_tools()))
    return registry
