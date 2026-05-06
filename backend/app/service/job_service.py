from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session
from models.job import Job, JobStatus, JobType
from exceptions.base import ResourceNotFoundException, PermissionDeniedException, APIException


class JobService:
    def create_job(
        self,
        db: Session,
        *,
        user_id: int,
        kb_id: int,
        type: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Job:
        job = Job(
            user_id=user_id,
            knowledge_base_id=kb_id,
            type=type,
            status=JobStatus.PENDING.value,
            progress=0,
            total=0,
            succeeded=0,
            failed=0,
            payload=payload or {},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def get_job(self, db: Session, *, job_id: int, user_id: int) -> Job:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            raise ResourceNotFoundException("Job not found")
        if job.user_id != user_id:
            raise PermissionDeniedException("No permission to access this job")
        return job

    def list_jobs(self, db: Session, *, user_id: int, kb_id: Optional[int] = None) -> List[Job]:
        q = db.query(Job).filter(Job.user_id == user_id)
        if kb_id is not None:
            q = q.filter(Job.knowledge_base_id == kb_id)
        return q.order_by(Job.id.desc()).all()

    def update_progress(
        self,
        db: Session,
        *,
        job_id: int,
        user_id: int,
        status: Optional[str] = None,
        progress: Optional[int] = None,
        total: Optional[int] = None,
        succeeded: Optional[int] = None,
        failed: Optional[int] = None,
        error: Optional[str] = None,
    ) -> Job:
        job = self.get_job(db, job_id=job_id, user_id=user_id)
        if status is not None:
            job.status = status
        if progress is not None:
            job.progress = progress
        if total is not None:
            job.total = total
        if succeeded is not None:
            job.succeeded = succeeded
        if failed is not None:
            job.failed = failed
        if error is not None:
            job.error = error
        db.add(job)
        db.commit()
        db.refresh(job)
        return job


job_service = JobService()


