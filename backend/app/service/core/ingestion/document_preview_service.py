"""Document preview service."""

from __future__ import annotations

import os
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from exceptions.base import PermissionDeniedException, ResourceNotFoundException
from models.user import User
from service import document_service, knowledgebase_service


class DocumentPreviewService:
    """Preview and download document files."""

    def __init__(self, *, db: Session, current_user: User) -> None:
        """Initialize the preview service.

        Args:
            db (Session): SQLAlchemy session.
            current_user (User): Authenticated user.
        """
        self.db = db
        self.current_user = current_user

    def preview_pdf(self, *, kb_id: int, doc_id: int) -> FileResponse:
        """Return PDF file response for a document.

        Args:
            kb_id (int): Knowledge base id.
            doc_id (int): Document id.

        Returns:
            FileResponse: PDF response.
        """
        try:
            knowledgebase_service.get_kb_by_id(
                db=self.db, kb_id=kb_id, user_id=self.current_user.id
            )
        except (ResourceNotFoundException, PermissionDeniedException) as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

        try:
            doc = document_service.get_document_by_id(
                db=self.db, doc_id=doc_id, user_id=self.current_user.id, kb_id=kb_id
            )
        except (ResourceNotFoundException, PermissionDeniedException) as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        file_path = doc.local_pdf_path
        if not file_path:
            raise HTTPException(status_code=404, detail="PDF file not found for this document")
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail=f"PDF file does not exist at path: {file_path}")

        filename = doc.title or f"document_{doc_id}.pdf"
        if not filename.endswith(".pdf"):
            filename += ".pdf"

        ascii_fallback = (
            "".join(char if ord(char) < 128 else "_" for char in filename)
            or f"document_{doc_id}.pdf"
        )
        if not ascii_fallback.endswith(".pdf"):
            ascii_fallback += ".pdf"

        content_disposition = (
            f"inline; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"
        )

        return FileResponse(
            path=file_path,
            media_type="application/pdf",
            headers={"Content-Disposition": content_disposition},
        )
