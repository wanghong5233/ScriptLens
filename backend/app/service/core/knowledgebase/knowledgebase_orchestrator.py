"""Knowledge base orchestration service."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from exceptions.base import PermissionDeniedException, ResourceNotFoundException
from models.user import User
from schemas.knowledge_base import KnowledgeBaseCreate, KnowledgeBaseUpdate
from service import knowledgebase_service


class KnowledgeBaseOrchestrator:
    """Handle knowledge base operations."""

    def __init__(self, *, db: Session, current_user: User) -> None:
        """Initialize orchestrator."""
        self.db = db
        self.current_user = current_user

    def create(self, kb_in: KnowledgeBaseCreate):
        """Create knowledge base."""
        return knowledgebase_service.create_kb_for_user(
            db=self.db,
            kb_create=kb_in,
            user_id=self.current_user.id,
        )

    def list(self):
        """List knowledge bases."""
        return knowledgebase_service.list_kbs_by_user_id(
            db=self.db,
            user_id=self.current_user.id,
        )

    def get(self, kb_id: int):
        """Get a knowledge base."""
        try:
            return knowledgebase_service.get_kb_by_id(
                db=self.db, kb_id=kb_id, user_id=self.current_user.id
            )
        except (ResourceNotFoundException, PermissionDeniedException) as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    def update(self, kb_id: int, kb_in: KnowledgeBaseUpdate):
        """Update a knowledge base."""
        try:
            return knowledgebase_service.update_kb(
                db=self.db,
                kb_id=kb_id,
                kb_update=kb_in,
                user_id=self.current_user.id,
            )
        except (ResourceNotFoundException, PermissionDeniedException) as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    def delete(self, kb_id: int):
        """Delete a knowledge base."""
        try:
            return knowledgebase_service.delete_kb(
                db=self.db, kb_id=kb_id, user_id=self.current_user.id
            )
        except (ResourceNotFoundException, PermissionDeniedException) as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    def cleanup_ephemeral(self, *, older_than_hours: int) -> dict:
        """Cleanup ephemeral knowledge bases."""
        if older_than_hours <= 0 or older_than_hours > 24 * 365:
            raise HTTPException(status_code=400, detail="olderThanHours 范围不合法")
        cleaned = knowledgebase_service.cleanup_ephemeral_kbs(
            self.db, self.current_user.id, older_than_hours
        )
        return {"cleaned": cleaned}
