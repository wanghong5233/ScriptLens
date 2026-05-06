"""Session upload service."""

from __future__ import annotations

import json
import os
import tempfile
from typing import List, Optional

from fastapi import BackgroundTasks, HTTPException, UploadFile
from sqlalchemy.orm import Session

from models.user import User
from schemas.knowledge_base import KnowledgeBaseCreate
from schemas.session import CreateSessionRequest
from service.core.api.utils.file_storage import FileStorageUtil
from service.core.ingestion.document_parser import LightweightDocumentParser
from service.job_handler.local_upload_handler import LocalUploadHandler
from service.job_runner_service import execute_job
from service.job_service import job_service
from service.knowledgebase_service import create_kb_for_user
from service.session_service import SessionService
from utils.get_logger import logger
from service.core.conversation.session_management_service import SessionManagementService


class SessionUploadService:
    """Handle session file uploads."""

    def __init__(self, *, db: Session, current_user: User) -> None:
        """Initialize the session upload service.

        Args:
            db (Session): SQLAlchemy session.
            current_user (User): Authenticated user.
        """
        self.db = db
        self.current_user = current_user
        self.session_service = SessionService(db)
        self.session_management = SessionManagementService(db=db, current_user=current_user)

    def create_and_upload(
        self,
        *,
        session_id: Optional[str],
        files: List[UploadFile] | None,
        file_single: UploadFile | None,
        background_tasks: BackgroundTasks,
    ):
        """Create a session and upload files."""
        if not session_id:
            req = CreateSessionRequest(kbId=None, ephemeral=True, defaults=None)
            resp = self.session_management.create_session(req=req)
            session_id = resp.sessionId
        return self.upload_by_session(
            session_id=session_id,
            background_tasks=background_tasks,
            files=files,
            file_single=file_single,
        )

    def upload_by_session(
        self,
        *,
        session_id: str,
        background_tasks: BackgroundTasks,
        files: List[UploadFile] | None,
        file_single: UploadFile | None,
    ):
        """Upload files under a session knowledge base."""
        s = self._get_session(session_id)
        kb_id = self._ensure_session_kb(s)
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
            raise HTTPException(status_code=400, detail=f"Unsupported file types: {', '.join(invalid)}")

        metas = []
        errors = []
        for f in up_files:
            try:
                metas.append(FileStorageUtil.save_upload_temp_session(f, self.current_user.id, session_id))
            except ValueError as ve:
                errors.append({"filename": f.filename, "error": str(ve)})
            except Exception:
                errors.append({"filename": f.filename, "error": "save failed"})

        if not metas and errors:
            raise HTTPException(status_code=413, detail={"message": "All files rejected", "errors": errors})

        job = job_service.create_job(
            self.db,
            user_id=self.current_user.id,
            kb_id=kb_id,
            type="UPLOAD_LOCAL",
            payload={"files": metas, "precheckErrors": errors, "sessionId": session_id},
        )

        background_tasks.add_task(
            execute_job,
            job_id=job.id,
            handler_cls=LocalUploadHandler,
        )

        return job

    def upload_for_context(
        self,
        *,
        session_id: str,
        file: UploadFile,
    ) -> dict:
        """Upload a file and extract content for session context."""
        s = self._get_session(session_id)
        if not file.filename:
            raise HTTPException(status_code=400, detail="文件名不能为空")

        allowed_exts = {".pdf", ".docx", ".txt"}
        if not any(file.filename.lower().endswith(ext) for ext in allowed_exts):
            raise HTTPException(status_code=400, detail=f"仅支持 {', '.join(allowed_exts)} 格式")

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as tmp:
                content = file.file.read()
                tmp.write(content)
                tmp_path = tmp.name

            parser = LightweightDocumentParser()
            blocks = parser.parse(file_path=tmp_path)

            extracted_text = "\n\n".join([block.text for block in blocks if block.text.strip()])
            max_chars = 400000
            if len(extracted_text) > max_chars:
                extracted_text = extracted_text[:max_chars] + "\n\n[文档内容过长，已截断]"

            os.remove(tmp_path)

            context_data = s.context_json or {}
            if isinstance(context_data, str):
                try:
                    context_data = json.loads(context_data)
                except Exception:
                    context_data = {}

            if "uploaded_files" not in context_data:
                context_data["uploaded_files"] = []

            context_data["uploaded_files"].append(
                {
                    "filename": file.filename,
                    "content": extracted_text,
                    "uploaded_at": __import__("datetime").datetime.utcnow().isoformat(),
                }
            )

            s.context_json = json.dumps(context_data, ensure_ascii=False)
            self.db.add(s)
            self.db.commit()

            logger.info(
                "[UPLOAD_FOR_CONTEXT] session=%s filename=%s content_len=%s total_files=%s",
                session_id,
                file.filename,
                len(extracted_text),
                len(context_data["uploaded_files"]),
            )

            return {"filename": file.filename, "content": extracted_text}
        except Exception as exc:
            if "tmp_path" in locals() and os.path.exists(tmp_path):
                os.remove(tmp_path)
            logger.error("文件内容提取失败: %s", exc)
            raise HTTPException(status_code=500, detail=f"文件内容提取失败: {str(exc)}") from exc

    def _get_session(self, session_id: str):
        s = self.session_service.get_session_by_id(session_id=session_id)
        if not s:
            raise HTTPException(status_code=404, detail="会话不存在")
        if str(self.current_user.id) != str(s.user_id):
            raise HTTPException(status_code=403, detail="无权操作该会话")
        return s

    def _ensure_session_kb(self, session_obj) -> int:
        """Ensure the session has a bound session knowledge base."""
        if session_obj.knowledge_base_id:
            return int(session_obj.knowledge_base_id)
        kb_name = f"session_kb_for_{session_obj.session_id}"
        kb = create_kb_for_user(
            db=self.db,
            kb_create=KnowledgeBaseCreate(name=kb_name, description=None, is_ephemeral=True),
            user_id=self.current_user.id,
        )
        session_obj.knowledge_base_id = kb.id
        self.db.add(session_obj)
        self.db.commit()
        self.db.refresh(session_obj)
        logger.info(
            "Backfilled session KB id=%s for legacy session %s",
            kb.id,
            session_obj.session_id,
        )
        return int(kb.id)
