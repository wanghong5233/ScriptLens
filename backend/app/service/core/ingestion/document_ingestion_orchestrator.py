"""Document ingestion orchestration service."""

from __future__ import annotations

import os
from typing import List

from fastapi import BackgroundTasks, HTTPException, status, UploadFile
from sqlalchemy.orm import Session

from core.config import settings
from exceptions.base import APIException, PermissionDeniedException, ResourceNotFoundException
from models.job import JobType
from models.user import User
from schemas.document import DocumentCreate
from schemas.job import JobInDB
from service import knowledgebase_service, document_service
from service.core.api.utils.file_storage import FileStorageUtil
from service.job_handler.local_upload_handler import LocalUploadHandler
from service.job_handler.parse_index_handler import ParseIndexHandler
from service.job_runner_service import execute_job
from service.job_service import job_service
from utils.quota import quota


class DocumentIngestionOrchestrator:
    """Orchestrate document ingestion workflows."""

    def __init__(self, *, db: Session, current_user: User) -> None:
        """Initialize the orchestrator.

        Args:
            db (Session): SQLAlchemy session.
            current_user (User): Authenticated user.
        """
        self.db = db
        self.current_user = current_user

    def upload_documents(
        self,
        *,
        kb_id: int,
        background_tasks: BackgroundTasks,
        files: List[UploadFile] | None,
        file_single: UploadFile | None,
    ) -> JobInDB:
        """Handle local file upload ingestion.

        Args:
            kb_id (int): Knowledge base id.
            background_tasks (BackgroundTasks): Background task queue.
            files (List[UploadFile] | None): Multiple files.
            file_single (UploadFile | None): Single file (alias=file).

        Returns:
            JobInDB: Created ingestion job.
        """
        try:
            knowledgebase_service.get_kb_by_id(
                db=self.db, kb_id=kb_id, user_id=self.current_user.id
            )
        except (ResourceNotFoundException, PermissionDeniedException) as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

        up_files: List[UploadFile] = []
        if file_single is not None:
            up_files.append(file_single)
        if files:
            up_files.extend(files)
        if not up_files:
            raise HTTPException(status_code=400, detail="No files provided")

        allowed_exts = {".pdf", ".docx", ".txt"}
        invalid = [
            f.filename
            for f in up_files
            if f and f.filename and (not any(f.filename.lower().endswith(ext) for ext in allowed_exts))
        ]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file types: {', '.join(invalid)}",
            )

        metas = []
        errors = []
        for f in up_files:
            try:
                metas.append(FileStorageUtil.save_upload_temp(f, kb_id))
            except ValueError as ve:
                errors.append({"filename": f.filename, "error": str(ve)})
            except Exception:
                errors.append({"filename": f.filename, "error": "save failed"})
        if not metas and errors:
            raise HTTPException(
                status_code=413, detail={"message": "All files rejected", "errors": errors}
            )

        try:
            total_bytes = sum(int(m.get("size", "0")) for m in metas)
        except Exception:
            total_bytes = 0
        day_key = f"upload:bytes:day:{self.current_user.id}:{int(__import__('time').time())//86400}"
        if not quota.consume_bytes(
            day_key,
            amount=total_bytes,
            limit=settings.DAILY_UPLOAD_MB * 1024 * 1024,
            window_seconds=86400,
        ):
            for m in metas:
                p = m.get("temp_path")
                try:
                    if p and os.path.isfile(p):
                        os.remove(p)
                except Exception:
                    continue
            raise HTTPException(status_code=429, detail="Daily upload quota exceeded")

        job = job_service.create_job(
            self.db,
            user_id=self.current_user.id,
            kb_id=kb_id,
            type=JobType.UPLOAD_LOCAL.value,
            payload={"files": metas, "precheckErrors": errors},
        )

        background_tasks.add_task(
            execute_job,
            job_id=job.id,
            handler_cls=LocalUploadHandler,
        )
        return job

    def parse_index_documents(
        self,
        *,
        kb_id: int,
        doc_ids: List[int] | None,
        background_tasks: BackgroundTasks,
        session_id: str | None = None,
    ) -> JobInDB:
        """Re-parse and index existing documents in a knowledge base.

        Args:
            kb_id (int): Knowledge base id.
            doc_ids (List[int] | None): Optional document ids to parse. If None, parse all.
            background_tasks (BackgroundTasks): Background task queue.
            session_id (str | None): Optional session id for traceability.

        Returns:
            JobInDB: Created parsing job.
        """
        try:
            knowledgebase_service.get_kb_by_id(
                db=self.db, kb_id=kb_id, user_id=self.current_user.id
            )
        except (ResourceNotFoundException, PermissionDeniedException) as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

        documents = document_service.list_documents_by_kb_id(
            db=self.db, kb_id=kb_id, user_id=self.current_user.id
        )
        all_doc_ids = {doc.id for doc in documents}

        if doc_ids:
            unique_ids = []
            seen = set()
            for doc_id in doc_ids:
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                unique_ids.append(doc_id)
            missing = [doc_id for doc_id in unique_ids if doc_id not in all_doc_ids]
            if missing:
                raise HTTPException(
                    status_code=404,
                    detail=f"Documents not found in this knowledge base: {missing}",
                )
            target_ids = unique_ids
        else:
            target_ids = list(all_doc_ids)

        if not target_ids:
            raise HTTPException(status_code=400, detail="No documents to parse.")

        job = job_service.create_job(
            self.db,
            user_id=self.current_user.id,
            kb_id=kb_id,
            type=JobType.PARSE_INDEX.value,
            payload={"docs": target_ids, "sessionId": session_id},
        )
        background_tasks.add_task(
            execute_job,
            job_id=job.id,
            handler_cls=ParseIndexHandler,
        )
        return job
