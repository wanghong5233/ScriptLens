from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Body, BackgroundTasks
from sqlalchemy.orm import Session
from models.user import User
from schemas.document import (
    DocumentInDB,
    DocumentUpdate,
    DocumentCreate,
    CriticalQuestionsResponse,
)
from service.auth import (
    get_current_user,
    get_current_user_optional_query_token,
)
from service import document_service
from service.core.ingestion.document_ingestion_orchestrator import DocumentIngestionOrchestrator
from service.core.ingestion.document_preview_service import DocumentPreviewService
from schemas.job import JobInDB
from utils.database import get_db
from exceptions.base import ResourceNotFoundException, PermissionDeniedException
from pydantic import BaseModel
from typing import List as _List
from fastapi import UploadFile, File
from service import knowledgebase_service
from core.config import settings
from service.core.conversation.document_question_service import DocumentQuestionService


router = APIRouter()

# DTO for online search request body
class OnlineSearchRequest(BaseModel):
    query: str
    limit: int = 100
    year: str = ""
    providers: Optional[List[str]] = None
    rank_by: Optional[str] = None

# DTO for add-online request body
class AddOnlineDocumentsRequest(BaseModel):
    documents: List[DocumentCreate]


class ParseIndexRequest(BaseModel):
    doc_ids: Optional[List[int]] = None
    session_id: Optional[str] = None
@router.post(
    "/upload",
    response_model=JobInDB,
    summary="本地上传文档（异步）",
    description="接收多文件上传，创建后台任务进行去重、持久化与落盘。"
)
def upload_documents(
    kb_id: int,
    background_tasks: BackgroundTasks,
    files: _List[UploadFile] = File(None),
    file_single: UploadFile | None = File(None, alias="file"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    orchestrator = DocumentIngestionOrchestrator(db=db, current_user=current_user)
    return orchestrator.upload_documents(
        kb_id=kb_id,
        background_tasks=background_tasks,
        files=files,
        file_single=file_single,
    )

@router.post(
    "/ingest/search-online",
    response_model=List[DocumentCreate],
    summary="在线检索学术论文",
    description="根据关键词从 Semantic Scholar 检索论文，返回待确认的论文列表。"
)
def search_online(
    kb_id: int,
    request: OnlineSearchRequest = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    orchestrator = DocumentIngestionOrchestrator(db=db, current_user=current_user)
    return orchestrator.search_online(
        kb_id=kb_id,
        query=request.query,
        limit=request.limit,
        year=request.year,
        providers=request.providers,
        rank_by=request.rank_by,
    )


@router.post(
    "/ingest/add-online",
    response_model=JobInDB,
    summary="异步添加在线检索的论文到知识库",
    description="创建后台任务：持久化并去重所选论文，并尝试下载PDF。返回Job以便轮询进度。"
)
def add_online_documents(
    kb_id: int,
    background_tasks: BackgroundTasks,
    payload: AddOnlineDocumentsRequest = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    orchestrator = DocumentIngestionOrchestrator(db=db, current_user=current_user)
    return orchestrator.add_online_documents(
        kb_id=kb_id,
        documents=payload.documents,
        background_tasks=background_tasks,
    )


@router.post(
    "/parse-index",
    response_model=JobInDB,
    summary="重新解析并入库",
    description="对已有文档重新解析并构建索引，适用于切换检索模式后的索引刷新。",
)
def parse_index_documents(
    kb_id: int,
    background_tasks: BackgroundTasks,
    payload: ParseIndexRequest = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    orchestrator = DocumentIngestionOrchestrator(db=db, current_user=current_user)
    return orchestrator.parse_index_documents(
        kb_id=kb_id,
        doc_ids=payload.doc_ids,
        background_tasks=background_tasks,
        session_id=payload.session_id,
    )


@router.post(
    "/{doc_id}/retry",
    response_model=JobInDB,
    summary="重试单个文档的解析入库",
    description=(
        "适用于 processing_status='failed' 的文档。"
        "先把状态重置为 pending，再调度 ParseIndexHandler 重新跑解析、分块、嵌入、索引。"
    ),
)
def retry_document(
    kb_id: int,
    doc_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        doc = document_service.get_document_by_id(db, doc_id, current_user.id, kb_id)
    except ResourceNotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionDeniedException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    if not doc.local_pdf_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="文档没有本地 PDF 文件，无法重试解析。请重新上传或重新导入。",
        )

    from service.document_lifecycle import reset_for_retry
    reset_for_retry(db, doc)

    orchestrator = DocumentIngestionOrchestrator(db=db, current_user=current_user)
    return orchestrator.parse_index_documents(
        kb_id=kb_id,
        doc_ids=[doc.id],
        background_tasks=background_tasks,
        session_id=None,
    )


@router.get(
    "/",
    response_model=List[DocumentInDB],
    summary="获取知识库中的所有文档",
    description="获取指定知识库下的所有文档列表。"
)
def list_documents(
    kb_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        return document_service.list_documents_by_kb_id(db, kb_id, current_user.id)
    except ResourceNotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionDeniedException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@router.patch(
    "/{doc_id}",
    response_model=DocumentInDB,
    summary="更新文档元数据",
    description="更新指定文档的元数据信息。"
)
def update_document_metadata(
    kb_id: int,
    doc_id: int,
    doc_update: DocumentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        # kb_id is used for both permission checks and filtering
        updated_document = document_service.update_document(db, doc_id, doc_update, current_user.id, kb_id)
        return updated_document
    except ResourceNotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionDeniedException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@router.delete(
    "/{doc_id}",
    response_model=DocumentInDB,
    summary="删除知识库中的文档",
    description="从知识库中删除指定的文档。"
)
def delete_document(
    kb_id: int,
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        # kb_id is used for permission checks
        deleted_document = document_service.delete_document(db, doc_id, current_user.id, kb_id)
        return deleted_document
    except ResourceNotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionDeniedException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "/{doc_id}/critical_questions",
    response_model=CriticalQuestionsResponse,
    summary="批判性问题生成",
    description="基于指定文档进行聚焦检索，生成若干高价值批判性问题（不调用 LLM 生成答案，仅输出问题）。"
)
def generate_critical_questions(
    kb_id: int,
    doc_id: int,
    top_n: int = 6,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = DocumentQuestionService(db=db, current_user=current_user)
    return service.generate_critical_questions(
        kb_id=kb_id,
        doc_id=doc_id,
        top_n=top_n,
    )


@router.get(
    "/{doc_id}/preview",
    summary="预览/下载 PDF 文件",
    description="返回文档的 PDF 文件，可在浏览器中预览或下载"
)
def preview_document(
    kb_id: int,
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional_query_token),
):
    service = DocumentPreviewService(db=db, current_user=current_user)
    return service.preview_pdf(kb_id=kb_id, doc_id=doc_id)
