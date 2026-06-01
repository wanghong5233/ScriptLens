"""
工具系统基础框架
定义工具接口和工具注册表
"""
import json
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    summary: Optional[str] = None


class BaseTool(ABC):
    """
    工具基类
    所有工具必须继承此类并实现 execute 方法
    """
    
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        # 工具参数定义（用于 LLM Tool Calling）
        self.parameters_schema: Dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": []
        }
    
    @abstractmethod
    async def execute(
        self,
        agent_state: Any,  # AgentState (使用 Any 避免循环导入)
        parameters: Dict[str, Any]
    ) -> ToolResult:
        """
        执行工具
        
        Args:
            agent_state: Agent 当前状态
            parameters: 工具参数
            
        Returns:
            工具执行结果
        """
        pass
    
    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """
        验证参数（可选实现）
        
        Returns:
            参数是否有效
        """
        return True

    def convergence_key(self, parameters: Dict[str, Any]) -> str:
        """
        Compute a stable fingerprint of `parameters` used by the agent
        same-tool-convergence guard to detect "the model is calling the same
        tool with the same inputs in a tight loop".

        Default: "" (empty string), which means the guard collapses to
        "same tool name N times in a row" — the original, conservative
        behavior before per-tool fingerprints existed.

        Why the default is intentionally degenerate:
        --------------------------------------------
        We tried `json.dumps(parameters)` as the default, but real LLM loops
        (e.g. rewrite_selection_scene_tool emitting token-level garbage and
        the model "trying again" with a slightly-edited expected_changes
        string) end up with different fingerprints on every iteration, so
        the guard never fires and the run only stops on max_iterations. The
        observed failure mode is the model burning the full iteration
        budget while the file accumulates degenerate content.

        Tools that DO legitimately get called many times with different
        identity-bearing arguments (e.g. one call per scene_id in batch
        rewrite) MUST override this to return only those identity fields
        (typically scene_id / file_path). The override widens the
        fingerprint domain so different scenes do not collide into a false
        convergence trip.
        """
        del parameters  # default keeps guardrail purely tool-name-based
        return ""


class ToolRegistry:
    """
    工具注册表
    管理所有可用工具
    """
    
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
    
    def register(self, tool: BaseTool):
        """注册工具"""
        self._tools[tool.name] = tool
        logger.info(f"Registered tool: {tool.name}")
    
    def get_tool(self, tool_name: str) -> Optional[BaseTool]:
        """获取工具"""
        return self._tools.get(tool_name)
    
    def list_tools(self) -> List[Dict[str, str]]:
        """列出所有工具"""
        return [
            {
                "name": tool.name,
                "description": tool.description
            }
            for tool in self._tools.values()
        ]
    
    def get_tools_for_llm(self) -> List[Dict[str, Any]]:
        """
        获取工具列表（用于 LLM Tool Calling）
        返回 OpenAI Tool Calling 格式的工具描述
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_schema
                }
            }
            for tool in self._tools.values()
        ]

