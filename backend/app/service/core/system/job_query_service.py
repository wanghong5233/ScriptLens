"""Job query service."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from models.user import User
from schemas.job import JobInDB
from service.job_service import job_service


class JobQueryService:
    """Query job data."""

    def __init__(self, *, db: Session, current_user: User) -> None:
        """Initialize job query service."""
        self.db = db
        self.current_user = current_user

    def list_jobs(self, *, kb_id: Optional[int]) -> list[JobInDB]:
        """List jobs for the current user."""
        jobs = job_service.list_jobs(self.db, user_id=self.current_user.id, kb_id=kb_id)
        return [JobInDB.model_validate(job) for job in jobs]

    def get_job(self, *, job_id: int) -> JobInDB:
        """Get job detail."""
        job = job_service.get_job(self.db, job_id=job_id, user_id=self.current_user.id)
        return JobInDB.model_validate(job)
