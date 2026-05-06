"""
检索类工具
"""
from typing import Dict, Any, List
import asyncio
import logging

from .base_tool import BaseTool, ToolResult
from ..rag_api_client import get_rag_api_client

logger = logging.getLogger(__name__)


class SearchPapersTool(BaseTool):
    """
    搜索论文工具
    在知识库中检索相关论文
    """
    
    def __init__(self):
        super().__init__(
            name="search_papers_tool",
            description="在知识库中检索相关论文，返回最相关的论文列表"
        )
        self.rag_client = get_rag_api_client()
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询文本"
                },
                "kb_id": {
                    "type": ["integer", "null"],
                    "description": "知识库 ID（可选）"
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回数量（默认 5）",
                    "default": 5
                }
            },
            "required": ["query"]
        }
    
    async def execute(
        self,
        agent_state: Any,
        parameters: Dict[str, Any]
    ) -> ToolResult:
        """
        执行检索
        
        Args:
            parameters:
                - query: 查询文本
                - kb_id: 知识库 ID
                - top_k: 返回数量（默认 5）
        """
        query = parameters.get("query", "")
        kb_id = parameters.get("kb_id") or getattr(agent_state, "knowledge_base_id", None)
        top_k = parameters.get("top_k", 5)
        
        if not query:
            return ToolResult(
                success=False,
                error="Query parameter is required"
            )
        
        if not kb_id:
            logger.info("SearchPapersTool: 未绑定知识库，跳过检索")
            return ToolResult(
                success=True,
                data={"papers": [], "skipped": True, "reason": "knowledge_base_missing"},
                summary="未绑定知识库，跳过检索"
            )
        
        try:
            # 使用统一的 RAG API 客户端
            results = await self.rag_client.retrieve(
                query=query,
                kb_id=kb_id,
                user_id=agent_state.user_id,
                top_k=top_k
            )
            
            return ToolResult(
                success=True,
                data={
                    "papers": results,
                    "count": len(results)
                },
                summary=f"Found {len(results)} relevant papers"
            )
        
        except Exception as e:
            logger.error(f"Search papers failed: {e}", exc_info=True)
            return ToolResult(
                success=False,
                error=str(e)
            )


class BatchSearchPapersTool(BaseTool):
    """
    批量搜索论文工具
    并行检索多个查询
    """
    
    def __init__(self):
        super().__init__(
            name="batch_search_papers_tool",
            description="批量检索多个查询，返回每个查询的相关论文列表"
        )
        self.rag_client = get_rag_api_client()
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "查询列表"
                },
                "kb_id": {
                    "type": ["integer", "null"],
                    "description": "知识库 ID（可选）"
                },
                "top_k": {
                    "type": "integer",
                    "description": "每个查询返回数量（默认 5）",
                    "default": 5
                }
            },
            "required": ["queries"]
        }
    
    async def execute(
        self,
        agent_state: Any,
        parameters: Dict[str, Any]
    ) -> ToolResult:
        """
        执行批量检索
        
        Args:
            parameters:
                - queries: 查询列表
                - kb_id: 知识库 ID
                - top_k: 每个查询返回数量
        """
        queries = [q for q in parameters.get("queries", []) if q]
        kb_id = parameters.get("kb_id") or getattr(agent_state, "knowledge_base_id", None)
        top_k = parameters.get("top_k", 5)
        
        if not queries:
            return ToolResult(
                success=False,
                error="Queries parameter is required"
            )
        
        if not kb_id:
            logger.info("BatchSearchPapersTool: 未绑定知识库，跳过检索")
            return ToolResult(
                success=True,
                data={"results": [], "skipped": True, "reason": "knowledge_base_missing"},
                summary="未绑定知识库，跳过检索"
            )
        
        logger.info("Batch searching %s queries for kb %s", len(queries), kb_id)
        
        try:
            results = await self._batch_retrieve(
                queries=queries,
                kb_id=kb_id,
                top_k=top_k,
                user_id=getattr(agent_state, "user_id", "0")
            )
        except Exception as exc:
            logger.error("Batch search failed: %s", exc, exc_info=True)
            return ToolResult(success=False, error=str(exc))
        
        success = all(item["success"] for item in results) if results else True
        summary = (
            f"批量检索完成：成功 {sum(1 for r in results if r['success'])} / {len(results)}"
        )
        
        return ToolResult(
            success=success,
            data={"results": results},
            summary=summary
        )
    
    async def _batch_retrieve(
        self,
        queries: List[str],
        kb_id: int,
        top_k: int,
        user_id: Any
    ) -> List[Dict[str, Any]]:
        """并行检索多个查询"""
        tasks = [
            self._retrieve_single_query(query, kb_id, top_k, user_id)
            for query in queries
        ]
        return await asyncio.gather(*tasks)
    
    async def _retrieve_single_query(
        self,
        query: str,
        kb_id: int,
        top_k: int,
        user_id: Any
    ) -> Dict[str, Any]:
        """检索单个查询"""
        try:
            # 使用统一的 RAG API 客户端
            papers = await self.rag_client.retrieve(
                query=query,
                kb_id=kb_id,
                user_id=int(user_id),
                top_k=top_k
            )
            
            return {
                "query": query,
                "success": True,
                "papers": papers,
                "count": len(papers)
            }
        except Exception as exc:
            logger.warning("Query %s failed: %s", query, exc)
            return {
                "query": query,
                "success": False,
                "error": str(exc)
            }

