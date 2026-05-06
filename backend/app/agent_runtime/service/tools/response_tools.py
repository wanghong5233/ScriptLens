"""
响应类工具
用于 Agent 回复用户、总结操作等
"""
import re
from typing import Dict, Any
import logging

from .base_tool import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class ReplyToUserTool(BaseTool):
    """
    回复用户工具
    
    用于 Agent 向用户生成回复，可以：
    - 回答用户的问题
    - 总结执行的操作
    - 解释修改内容
    - 提供建议
    
    这个工具应该在任务完成时调用，是 Agent 执行流程的最后一步
    """
    
    def __init__(self):
        super().__init__(
            name="reply_to_user_tool",
            description=(
                "向用户生成回复消息。用于：\n"
                "1. 回答用户的问题\n"
                "2. 总结已执行的操作（如果修改了文件）\n"
                "3. 解释为什么做这些修改\n"
                "4. 提供后续建议\n"
                "这应该是任务执行的最后一步，在所有编辑/分析完成后调用。"
            )
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "reply": {
                    "type": "string",
                    "description": (
                        "回复内容，应包含：\n"
                        "- 已完成的操作摘要\n"
                        "- 修改的文件列表（如果有）\n"
                        "- 对用户问题的回答\n"
                        "- 后续建议（可选）"
                    )
                },
                "summary": {
                    "type": "string",
                    "description": "操作摘要（简短版本，用于日志）"
                }
            },
            "required": ["reply"]
        }
    
    async def execute(
        self,
        agent_state: Any,
        parameters: Dict[str, Any]
    ) -> ToolResult:
        """
        执行回复
        
        Args:
            parameters:
                - reply: 回复内容（必需）
                - summary: 操作摘要（可选）
        
        Returns:
            ToolResult 包含回复内容
        """
        reply = parameters.get("reply", "")
        summary = parameters.get("summary", "已完成")
        
        if not reply.strip():
            return ToolResult(
                success=False,
                error="Reply content cannot be empty"
            )
        
        # 记录回复日志
        logger.info(f"Agent reply: {summary}")
        logger.debug(f"Full reply: {reply[:200]}...")
        # 调试：输出原始内容中的换行结构，便于排查渲染间距问题
        nl_count = reply.count("\n")
        double_nl = reply.count("\n\n")
        triple_plus = len(list(re.finditer(r"\n{3,}", reply)))
        logger.info(
            "Agent reply raw stats: len=%d, \\n=%d, \\n\\n=%d, \\n{3+}=%d, sample_repr=%s",
            len(reply), nl_count, double_nl, triple_plus,
            repr(reply[:300]) if len(reply) > 300 else repr(reply),
        )
        
        # 返回回复内容
        return ToolResult(
            success=True,
            data={
                "reply": reply,
                "summary": summary,
                "modified_files": list(getattr(agent_state, "modified_files", set()))
            },
            summary=summary
        )

