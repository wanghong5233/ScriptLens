from typing import List
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session
from models.user import User
from schemas.knowledge_base import KnowledgeBaseInDB, KnowledgeBaseCreate, KnowledgeBaseUpdate
from service.auth import get_current_user
from utils.database import get_db
from pydantic import BaseModel

from service.core.knowledgebase.knowledgebase_orchestrator import KnowledgeBaseOrchestrator


router = APIRouter()

@router.post("/", response_model=KnowledgeBaseInDB, status_code=status.HTTP_201_CREATED)
def create_knowledge_base(
    *,
    db: Session = Depends(get_db),
    kb_in: KnowledgeBaseCreate,
    current_user: User = Depends(get_current_user)
):
    """
    为当前认证的用户创建一个新的知识库。
    """
    service = KnowledgeBaseOrchestrator(db=db, current_user=current_user)
    return service.create(kb_in)

@router.get("/", response_model=List[KnowledgeBaseInDB])
def list_knowledge_bases(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    获取当前认证用户的所有知识库列表。
    """
    service = KnowledgeBaseOrchestrator(db=db, current_user=current_user)
    return service.list()

@router.get("/{kb_id}", response_model=KnowledgeBaseInDB)
def get_knowledge_base(
    kb_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    获取指定ID的知识库详情。
    """
    service = KnowledgeBaseOrchestrator(db=db, current_user=current_user)
    return service.get(kb_id)

@router.patch("/{kb_id}", response_model=KnowledgeBaseInDB)
def update_knowledge_base(
    kb_id: int,
    kb_in: KnowledgeBaseUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    更新指定ID的知识库。
    """
    service = KnowledgeBaseOrchestrator(db=db, current_user=current_user)
    return service.update(kb_id, kb_in)

@router.delete("/{kb_id}", response_model=KnowledgeBaseInDB)
def delete_knowledge_base(
    kb_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    删除指定ID的知识库。
    """
    service = KnowledgeBaseOrchestrator(db=db, current_user=current_user)
    return service.delete(kb_id)


class CleanupRequest(BaseModel):
    olderThanHours: int = 24

@router.post(
    "/cleanup-ephemeral",
    summary="清理过期的会话知识库",
    description="按时间阈值（小时）清理当前用户的会话知识库（ephemeral）及其文档/向量等资源。",
)
def cleanup_ephemeral_kbs(
    payload: CleanupRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = KnowledgeBaseOrchestrator(db=db, current_user=current_user)
    return service.cleanup_ephemeral(older_than_hours=payload.olderThanHours)

