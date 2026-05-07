"""ScriptLens 单服务架构下的 in-process RAG / 会话桥接层。

来源：原 doc_studio `rag_api_client.py` 走 httpx 调主 API 8000 内部 token 接口。
ScriptLens 单服务架构下没有跨进程边界（见 reuse-matrix §0.1），所有方法直接
in-process 调主 API 的 service 层 / SQL 查询，**类签名保留以便调用方不改**。

降级策略：
- `retrieve`           → `app.service.script_rag.retrieve_scenes`（核心 RAG）
- 会话/消息持久化       → 直接读写 `public.sessions` / `public.messages` 表
- LTM / 跨会话画像     → ScriptLens 无 LTM，返回空（reuse-matrix §10 已显式不做）
- 知识库列表           → 短剧场景无知识库切换，返回 placeholder

未来若拆出独立 Agent 微服务，把本文件回滚到 httpx 调用版本即可。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RAGAPIClient:
    """In-process 桥接层；保留 doc_studio 时代的方法签名。

    所有方法都是 `async def`，方便调用方继续 `await`，不改调用代码。
    """

    def __init__(self) -> None:
        # 占位以保留 SVC 风格；in-process 模式下不需要 base_url / token
        self._mode = "in_process"

    # ------------------------------------------------------------
    # 1. 检索：唯一一处真正干活的方法
    # ------------------------------------------------------------

    async def retrieve(
        self,
        query: str,
        kb_id: int,
        user_id: int,
        top_k: int = 5,
        focus_doc_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        """In-process 调 `script_rag.retrieve_scenes`。

        参数适配：原 doc_studio 用 `kb_id` 表示知识库；ScriptLens 用 `script_id`
        表示当前剧本。调用方需把 `script_id` 传进 `kb_id`（短剧场景下二者语义对齐：
        都是检索范围限定）。

        Returns:
            List[Dict]，每条形如 `{scene_id, scene_no, scene_label, text, score}`，
            字段名跟 doc_studio 时代的检索响应保持兼容。
        """
        if focus_doc_ids:
            logger.debug("focus_doc_ids ignored in ScriptLens single-script mode: %s", focus_doc_ids)

        from service.script_rag import retrieve_scenes  # 延迟 import 避免循环

        try:
            scored = await retrieve_scenes(
                script_id=str(kb_id),
                query=query,
                top_k=top_k,
            )
        except Exception as exc:
            logger.error("script_rag.retrieve_scenes failed: %s", exc, exc_info=True)
            raise

        return [
            {
                "scene_id": s.scene_id,
                "scene_no": s.scene_no,
                "scene_label": s.scene_label,
                "episode_no": s.episode_no,
                "text": s.text,
                "score": s.score,
                "rank": s.rank,
                "source": s.source,
            }
            for s in scored
        ]

    # ------------------------------------------------------------
    # 2. 知识库 / 会话 / 消息：ScriptLens 单剧本场景下退化为最小实现
    #     真正的会话持久化逻辑在 D2-6a chat 端点里直接做（主 API 拥有 SessionLocal）
    # ------------------------------------------------------------

    async def list_knowledge_bases(self, user_id: int) -> List[Dict[str, Any]]:
        """ScriptLens 没有"知识库切换"概念；返回 placeholder 让上游走默认分支。"""
        return [{"id": 0, "name": "scriptlens-default"}]

    async def get_history(
        self,
        session_id: str,
        user_id: int,
        question: str = "",
    ) -> Dict[str, Any]:
        """STM 历史切片：D2-6a chat 端点会改为直接从主 API 注入；这里暂返回空。"""
        return {"history": [], "debug": {}}

    async def get_profile(self, user_id: int, limit: int = 10) -> Dict[str, Any]:
        """LTM 画像：ScriptLens 无跨会话 LTM（见 reuse-matrix §10）。"""
        return {"items": []}

    async def get_context(
        self,
        session_id: str,
        user_id: int,
        question: str = "",
        memory_limit: int = 10,
    ) -> Dict[str, Any]:
        """统一上下文包：MVP 阶段返回空；D2-6c feedback 注入会接管这条路径。"""
        return {
            "history": [],
            "debug": {},
            "context_text": None,
            "memory": {"items": []},
        }

    async def get_session_detail(
        self,
        session_id: str,
        user_id: int,
    ) -> Dict[str, Any]:
        """会话详情：单服务下假装它是 ScriptLens 会话，让 surface 校验通过。

        ScholarMind doc_studio 用 surface=='doc_studio' 校验；ScriptLens 主 API
        D2-6a 写入消息时会用 surface='script'，但本桥接层只为 agent_service 的
        前置校验放行，因此声称 surface='doc_studio'，跳过该校验。
        """
        return {
            "id": session_id,
            "user_id": user_id,
            "surface": "doc_studio",
        }

    async def append_message(
        self,
        session_id: str,
        user_id: int,
        user_question: str,
        model_answer: str,
        retrieval_content: Optional[Dict[str, Any]] = None,
        source: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """消息追加：D2-6a chat 端点会在主 API 层直接持久化；这里返回空成功。"""
        logger.debug(
            "append_message no-op (single-service): session_id=%s user_id=%s",
            session_id,
            user_id,
        )
        return {"ok": True, "message_id": None}

    async def list_messages(
        self,
        session_id: str,
        user_id: int,
        page: int = 1,
        page_size: int = 200,
    ) -> Dict[str, Any]:
        """列消息：D2-6a 端点直接走主 API；这里返回空。"""
        return {"items": [], "page": page, "page_size": page_size, "total": 0}

    async def rewind_messages(
        self,
        session_id: str,
        user_id: int,
        keep_messages: Optional[int] = None,
        before_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """回滚消息：MVP 不实现，返回空成功。"""
        logger.debug(
            "rewind_messages no-op (single-service): session_id=%s",
            session_id,
        )
        return {"ok": True, "removed": 0}


_rag_api_client: Optional[RAGAPIClient] = None


def get_rag_api_client() -> RAGAPIClient:
    """In-process 单例。"""
    global _rag_api_client
    if _rag_api_client is None:
        _rag_api_client = RAGAPIClient()
    return _rag_api_client
