"""History service for legacy history endpoints."""

from __future__ import annotations

from typing import List

from fastapi import HTTPException, status
from fastapi_jwt import JwtAuthorizationCredentials
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from models.knowledgebase import KnowledgeBase
from schemas.message import FilestResponse, SessionListResponse, SessionResponse
from service.core.conversation.internal_history_filter import (
    is_internal_deep_research_artifact,
    purge_internal_deep_research_artifacts,
)


class HistoryService:
    """Provide access to legacy history data."""

    def __init__(self, *, db: Session, credentials: JwtAuthorizationCredentials) -> None:
        """Initialize the history service.

        Args:
            db (Session): SQLAlchemy session.
            credentials (JwtAuthorizationCredentials): JWT credentials.
        """
        self.db = db
        self.credentials = credentials

    def get_documents_by_user_id(self) -> List[FilestResponse]:
        """Return documents for the authenticated user."""
        try:
            user_id = self._get_user_id()
            stmt = select(KnowledgeBase).where(KnowledgeBase.user_id == user_id)
            result = self.db.execute(stmt).scalars().all()
            if not result:
                return []
            documents = [
                FilestResponse(
                    user_id=row.user_id,
                    file_name=row.file_name,
                    created_at=row.created_at.isoformat(),
                    updated_at=row.updated_at.isoformat(),
                )
                for row in result
            ]
            return documents
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to retrieve documents: {str(exc)}",
            ) from exc

    def get_messages_by_session_id(self, *, session_id: str):
        """Return messages for a session."""
        try:
            user_id = self._get_user_id()
            if not user_id:
                raise HTTPException(status_code=401, detail="Invalid authentication credentials")

            purge_internal_deep_research_artifacts(self.db, session_id=session_id)

            messages_data = self.db.execute(
                text(
                    """
                    SELECT message_id,
                           session_id,
                           user_question,
                           model_answer,
                           retrieval_content,
                           create_time
                      FROM messages
                     WHERE session_id = :session_id
                     ORDER BY create_time ASC
                    """
                ),
                {"session_id": session_id},
            ).fetchall()

            messages = []
            import json as _json
            for m in messages_data:
                if is_internal_deep_research_artifact(
                    user_question=m.user_question,
                    model_answer=m.model_answer,
                    retrieval_content=m.retrieval_content,
                ):
                    continue
                docs_field = None
                try:
                    if m.retrieval_content:
                        rc = _json.loads(m.retrieval_content)
                        cits = rc.get("citations") or []
                        if isinstance(cits, list):
                            docs_field = _json.dumps(cits, ensure_ascii=False)
                except Exception:
                    docs_field = None

                messages.append(
                    {
                        "message_id": m.message_id,
                        "session_id": m.session_id,
                        "user_question": m.user_question,
                        "model_answer": m.model_answer,
                        "documents": docs_field,
                        "recommended_questions": None,
                        "think": None,
                        "created_at": m.create_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "retrieval_content": m.retrieval_content,
                    }
                )

            return messages
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to retrieve messages: {str(exc)}",
            ) from exc

    def get_sessions_by_user_id(self, *, surface: str | None = None) -> SessionListResponse:
        """Return sessions for the authenticated user."""
        try:
            user_id = self._get_user_id()
            if not user_id:
                raise HTTPException(status_code=401, detail="Invalid authentication credentials")

            if surface:
                sessions_data = self.db.execute(
                    text(
                        """
                        SELECT *
                          FROM sessions
                         WHERE user_id = :user_id
                           AND surface = :surface
                        """
                    ),
                    {"user_id": user_id, "surface": surface},
                ).fetchall()
            else:
                sessions_data = self.db.execute(
                    text("SELECT * FROM sessions WHERE user_id = :user_id"),
                    {"user_id": user_id},
                ).fetchall()

            sessions = []
            for session in sessions_data:
                sessions.append(
                    SessionResponse(
                        session_id=session.session_id,
                        session_name=session.session_name,
                        user_id=session.user_id,
                        surface=(getattr(session, "surface", None) or "deep_chat"),
                        created_at=session.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                        updated_at=session.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                )
            return {"user_id": user_id, "sessions": sessions}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=str(exc),
            ) from exc

    def _get_user_id(self) -> str:
        user_id = str(self.credentials.subject.get("user_id"))
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
            )
        return user_id
