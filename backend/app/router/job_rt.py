from typing import List, Optional, Any
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from models.user import User
from utils.database import get_db
from service.auth import get_current_user
from schemas.job import JobInDB
from service.core.system.job_query_service import JobQueryService


router = APIRouter()


@router.get("/", response_model=List[JobInDB], summary="列出当前用户的任务")
def list_jobs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    kb_id: Optional[int] = Query(None),
):
    service = JobQueryService(db=db, current_user=current_user)
    return service.list_jobs(kb_id=kb_id)


@router.get("/{job_id}", response_model=JobInDB, summary="查询任务详情")
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = JobQueryService(db=db, current_user=current_user)
    return service.get_job(job_id=job_id)


