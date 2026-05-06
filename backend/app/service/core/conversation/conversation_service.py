"""Shared conversation context utilities.

ScriptLens MVP 简化版：
- 删除全部 long-term memory / fact extraction / KG / cross-session recall 路径
  （这些是 ScholarMind 的论文场景能力，剧本场景下不需要）
- 仅保留 short-term memory（同会话内最近 N 轮）作为历史片段
- list_memory_profile / maybe_update_rolling_summary 保留接口签名，返回空 / no-op，
  便于 internal_rt 路由保持原有契约不变
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from service.core.rag.history.short_term_memory import (
    ShortTermMemoryBuilder,
    ShortTermMemoryDebug,
)
from service.session_service import SessionService


class ConversationService:
    """Provide shared conversation context utilities (simplified for ScriptLens)."""

    def __init__(
        self,
        db: Session,
        *,
        stm_builder: Optional[ShortTermMemoryBuilder] = None,
        ltm_service: Any = None,  # 兼容旧签名，MVP 不使用
        llm_client: Any = None,
        session_service: Optional[SessionService] = None,
    ) -> None:
        self.db = db
        self.stm_builder = stm_builder or ShortTermMemoryBuilder(db)
        self.session_service = session_service or SessionService(db)

    def build_history_slice(
        self,
        *,
        session_id: str,
        question: str,
        enable_semantic: bool = True,
    ) -> Tuple[List[Dict[str, str]], ShortTermMemoryDebug, Optional[List[float]]]:
        """Build a short-term memory slice for a session."""
        return self.stm_builder.build_history(
            session_id=session_id,
            question=question,
            enable_semantic=enable_semantic,
        )

    def build_context_pack(
        self,
        *,
        session_id: str,
        user_id: int | str,
        question: str,
        memory_limit: int = 10,
        history_limit: int = 12,
        memory_preview_limit: int = 80,
        max_text_chars: int = 4000,
        max_context_tokens: int = 1500,
    ) -> Dict[str, Any]:
        """Build a context pack containing history slice and rolling summary.

        ScriptLens MVP: memory_items 始终为空（无 LTM）。
        """
        history, debug, _ = self.build_history_slice(
            session_id=session_id,
            question=question,
            enable_semantic=True,
        )

        session_obj = self.session_service.get_session_by_id(session_id=session_id)
        rolling_summary = getattr(session_obj, "rolling_summary", None) if session_obj else None

        history_lines = [
            f"{turn.get('role', 'user')}: {turn.get('content', '')}"
            for turn in history[-history_limit:]
        ]
        context_text = "\n".join(history_lines)
        if rolling_summary:
            context_text = f"[Summary] {rolling_summary}\n{context_text}"
        if len(context_text) > max_text_chars:
            context_text = context_text[-max_text_chars:]

        return {
            "history": history,
            "debug": debug,
            "memory_items": [],
            "context_text": context_text,
            "rolling_summary": rolling_summary,
            "context_meta": {
                "memory_limit": memory_limit,
                "history_limit": history_limit,
                "max_text_chars": max_text_chars,
                "max_context_tokens": max_context_tokens,
            },
        }

    def maybe_update_rolling_summary(self, *, session_id: str) -> None:
        """ScriptLens MVP 暂不开启 rolling summary 自动更新。

        保留接口，避免 internal_rt 调用时报错。D2 若需要可接回 LLM 摘要逻辑。
        """
        return None

    def list_memory_profile(
        self,
        *,
        user_id: int | str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """ScriptLens MVP 无 LTM，画像始终为空列表。"""
        return []
