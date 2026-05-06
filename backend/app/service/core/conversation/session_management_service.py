"""Session management service."""

from __future__ import annotations

import json
import uuid
from typing import Any, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models.message import Message
from models.user import User
from schemas.knowledge_base import KnowledgeBaseCreate
from schemas.session import (
    CreateSessionRequest,
    CreateSessionResponse,
    SessionDefaults,
    SessionDetail,
)
from service.knowledgebase_service import create_kb_for_user, get_kb_by_id
from service.core.conversation.ask_run_control import get_ask_run_control
from service.core.conversation.internal_history_filter import (
    purge_internal_deep_research_artifacts,
)
from service.session_service import SessionService
from utils.get_logger import logger


class SessionManagementService:
    """Handle session CRUD and defaults management."""

    def __init__(self, *, db: Session, current_user: User) -> None:
        """Initialize the session management service.

        Args:
            db (Session): SQLAlchemy session.
            current_user (User): Authenticated user.
        """
        self.db = db
        self.current_user = current_user
        self.session_service = SessionService(db)

    def list_messages(self, *, session_id: str, page: int, page_size: int) -> dict[str, Any]:
        """List paginated messages for a session."""
        s = self._get_session(session_id)
        purge_internal_deep_research_artifacts(self.db, session_id=s.session_id)
        q = self.db.query(Message).filter(Message.session_id == s.session_id)
        total = q.count()
        items = (
            q.order_by(Message.create_time.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        out = [
            {
                "message_id": str(m.message_id),
                "session_id": m.session_id,
                "user_question": m.user_question,
                "model_answer": m.model_answer,
                "create_time": str(m.create_time),
                "retrieval_content": m.retrieval_content,
            }
            for m in items
        ]
        return {"total": total, "page": page, "pageSize": page_size, "items": out}

    def rewind_messages(
        self,
        *,
        session_id: str,
        keep_messages: Optional[int] = None,
        before_message_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Rewind a session to a prefix of messages.

        Two selection modes are supported:
        1) ``before_message_id``: keep messages strictly before this message id.
        2) ``keep_messages``: keep the first N messages.

        All later messages are deleted. Rolling summary/checkpoints are reset
        to prevent stale future-branch context from leaking into later turns.
        """
        logger.info(
            "Session rewind requested: session_id=%s user_id=%s keep_messages=%s before_message_id=%s",
            session_id,
            getattr(self.current_user, "id", None),
            keep_messages,
            before_message_id,
        )
        session_obj = self._get_session(session_id)
        messages = (
            self.db.query(Message)
            .filter(Message.session_id == session_obj.session_id)
            .order_by(Message.create_time.asc(), Message.message_id.asc())
            .all()
        )
        total = len(messages)
        kept = 0
        if before_message_id:
            before_id = str(before_message_id).strip().lower()
            matched_index = next(
                (
                    idx
                    for idx, item in enumerate(messages)
                    if str(item.message_id).strip().lower() == before_id
                ),
                None,
            )
            if matched_index is None:
                logger.warning(
                    "Session rewind rejected: message_id not found in session. session_id=%s before_message_id=%s",
                    session_id,
                    before_message_id,
                )
                raise HTTPException(status_code=404, detail="message_id not found in session")
            kept = matched_index
        else:
            safe_keep = max(int(keep_messages or 0), 0)
            kept = min(safe_keep, total)
        to_delete = messages[kept:]
        deleted = len(to_delete)

        if deleted > 0:
            delete_ids = [m.message_id for m in to_delete if m.message_id is not None]
            if delete_ids:
                (
                    self.db.query(Message)
                    .filter(Message.message_id.in_(delete_ids))
                    .delete(synchronize_session=False)
                )
            # Reset rolling summary on rewind (ScriptLens MVP has no checkpoint table).
            session_obj.rolling_summary = None
            self.db.commit()

        logger.info(
            "Session rewind completed: session_id=%s total=%s kept=%s deleted=%s",
            session_obj.session_id,
            total,
            kept,
            deleted,
        )

        return {
            "session_id": session_obj.session_id,
            "total_messages": total,
            "kept_messages": kept,
            "deleted_messages": deleted,
        }

    def create_session(self, *, req: CreateSessionRequest) -> CreateSessionResponse:
        """Create a new session."""
        session_id = f"session_{uuid.uuid4().hex[:8]}"
        defaults = req.defaults.model_copy() if req.defaults is not None else SessionDefaults()

        # 每个 session 必须绑定专属 Session KB（会话级知识库）。
        # 沿用 is_ephemeral 字段用于“会话级”标识与前端过滤，不做 message-only 会话。
        kb_name = f"session_kb_for_{session_id}"
        session_kb = create_kb_for_user(
            db=self.db,
            kb_create=KnowledgeBaseCreate(name=kb_name, description=None, is_ephemeral=True),
            user_id=self.current_user.id,
        )
        kb_id = session_kb.id
        logger.info("Created session KB id=%s for session %s", kb_id, session_id)

        if req.defaults is None:
            defaults.useSessionKnowledgeBase = True
            defaults.useUserKnowledgeBase = False
            defaults.userKnowledgeBaseId = None

        # 可选指定默认关联知识库（用户知识库），不替代 session_kb。
        if req.kbId is not None:
            get_kb_by_id(db=self.db, kb_id=req.kbId, user_id=self.current_user.id)
            defaults.useUserKnowledgeBase = True
            defaults.userKnowledgeBaseId = int(req.kbId)

        session_name = f"Session KB {kb_id}"
        self.session_service.create_session(
            session_id=session_id,
            user_id=self.current_user.id,
            knowledge_base_id=kb_id,
            session_name=session_name,
            defaults_json=json.dumps(defaults.model_dump(), ensure_ascii=False),
            surface=req.surface,
        )

        return CreateSessionResponse(
            sessionId=session_id,
            kbId=kb_id,
            ephemeral=True,
            defaults=defaults,
            surface=req.surface,
        )

    def get_session_detail(self, *, session_id: str) -> SessionDetail:
        """Return session detail."""
        s = self._get_session(session_id)
        return SessionDetail(
            sessionId=s.session_id,
            kbId=s.knowledge_base_id,
            sessionName=s.session_name,
            surface=str(getattr(s, "surface", "deep_chat") or "deep_chat"),
        )

    def rename_session(self, *, session_id: str, session_name: str) -> SessionDetail:
        """Rename session."""
        s = self._get_session(session_id)
        normalized_name = (session_name or "").strip()
        if not normalized_name:
            raise HTTPException(status_code=400, detail="会话名称不能为空")
        if len(normalized_name) > 120:
            raise HTTPException(status_code=400, detail="会话名称过长（最多 120 字符）")
        updated = self.session_service.update_session_name(
            session_id=s.session_id,
            session_name=normalized_name,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return SessionDetail(
            sessionId=updated.session_id,
            kbId=updated.knowledge_base_id,
            sessionName=updated.session_name,
            surface=str(getattr(updated, "surface", "deep_chat") or "deep_chat"),
        )

    def get_session_defaults(self, *, session_id: str) -> SessionDefaults:
        """Return session defaults."""
        s = self._get_session(session_id)
        if s.defaults_json:
            try:
                data = json.loads(s.defaults_json)
                return SessionDefaults(**data)
            except Exception:
                pass
        return SessionDefaults()

    def update_session_defaults(self, *, session_id: str, payload: SessionDefaults) -> SessionDefaults:
        """Update session defaults."""
        s = self._get_session(session_id)
        data = payload.model_dump()
        if data.get("useSessionKnowledgeBase"):
            if s.knowledge_base_id is None:
                raise HTTPException(status_code=400, detail="当前会话没有可用的会话知识库")
        if data.get("useUserKnowledgeBase"):
            user_kb_id = data.get("userKnowledgeBaseId")
            if user_kb_id is None:
                raise HTTPException(status_code=400, detail="启用本地知识库时必须选择知识库")
            get_kb_by_id(db=self.db, kb_id=user_kb_id, user_id=self.current_user.id)
        else:
            data["userKnowledgeBaseId"] = None

        normalized = SessionDefaults(**data)
        self.session_service.update_defaults_json(
            session_id=session_id,
            defaults_json=json.dumps(normalized.model_dump(), ensure_ascii=False),
        )
        return normalized

    def delete_session(self, *, session_id: str) -> dict[str, Any]:
        """Delete a session."""
        s = self._get_session(session_id)
        run_control = get_ask_run_control()
        cancelled_runs = run_control.cancel_runs_for_session(
            session_id=s.session_id,
            user_id=int(self.current_user.id),
        )
        if cancelled_runs:
            logger.info(
                "Cancelled %s active ask runs before deleting session %s",
                cancelled_runs,
                s.session_id,
            )
        try:
            return self.session_service.delete_session(session_id=s.session_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _get_session(self, session_id: str):
        s = self.session_service.get_session_by_id(session_id=session_id)
        if not s:
            raise HTTPException(status_code=404, detail="会话不存在")
        if str(self.current_user.id) != str(s.user_id):
            raise HTTPException(status_code=403, detail="无权访问该会话")
        return s
