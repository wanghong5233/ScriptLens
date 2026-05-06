from abc import ABC, abstractmethod
from typing import List, Dict, Any, AsyncGenerator
from schemas.rag import Chunk

# 定义一个简单的 Pydantic 模型来标准化工具调用的结果
from pydantic import BaseModel

class ToolCallResult(BaseModel):
    tool_name: str
    tool_arguments: Dict[str, Any]

class BaseLLM(ABC):
    """
    大语言模型 (LLM) 组件的抽象基类 (接口)。
    定义了所有 LLM 实现类必须遵循的统一规范，并为高级功能预留了接口。
    """

    @abstractmethod
    async def generate(self, query: str, context: List[Chunk]) -> str:
        """
        根据上下文生成标准、非流式的文本响应。

        Args:
            query (str): 用户的查询。
            context (List[Chunk]): 用于生成答案的上下文信息（检索到的文本块）。

        Returns:
            str: LLM 生成的完整答案字符串。
        """
        pass

    @abstractmethod
    async def stream_generate(self, query: str, context: List[Chunk]) -> AsyncGenerator[str, None]:
        """
        根据上下文生成流式文本响应。
        这对于实现打字机效果至关重要。

        Args:
            query (str): 用户的查询。
            context (List[Chunk]): 用于生成答案的上下文信息。

        Yields:
            str: LLM 生成的文本片段 (token)。
        """
        # 这是一个异步生成器，实现时需要使用 `async for` 循环
        # yield "dummy implementation"
        pass

    @abstractmethod
    async def generate_from_prompt(self, prompt: str) -> str:
        """
        直接根据一个已经构造好的完整 Prompt 生成非流式响应。

        Args:
            prompt (str): 包含所有上下文和指令的完整 Prompt 字符串。

        Returns:
            str: LLM 生成的完整答案字符串。
        """
        pass

    @abstractmethod
    async def stream_generate_from_prompt(self, prompt: str) -> AsyncGenerator[str, None]:
        """
        直接根据一个已经构造好的完整 Prompt 生成流式响应。

        Args:
            prompt (str): 包含所有上下文和指令的完整 Prompt 字符串。

        Yields:
            str: LLM 生成的文本片段 (token)。
        """
        pass

    @abstractmethod
    async def generate_with_tools(self, query: str, context: List[Chunk], tools: List[Dict]) -> ToolCallResult:
        """
        【为Phase 3 Agent功能预留】
        根据上下文和可用工具列表，决定是否调用一个工具。

        Args:
            query (str): 用户的查询。
            context (List[Chunk]): 上下文信息。
            tools (List[Dict]): 一个描述可用工具的字典列表（遵循OpenAI Tool Calling格式）。

        Returns:
            ToolCallResult: 一个包含被调用工具名称和参数的对象。
        """
        pass
