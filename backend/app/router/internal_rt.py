"""
内部服务 API 路由
专门用于服务间调用（如 Doc Studio）
"""
import json
from dataclasses import asdict
from typing import Any, Dict, Optional, List
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi_jwt import JwtAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from models.message import Message
from models.user import User
from schemas.rag import Chunk as RagChunk
from service.auth import access_security, is_internal_service_token_payload
from service.core.conversation.conversation_service import ConversationService
from service import knowledgebase_service
from service.session_service import SessionService
from service.core.conversation.session_management_service import SessionManagementService
from service.core.rag.retriever import RAGRetriever
from service.core.rag.providers.registry import resolve_provider
from utils.database import get_db, SessionLocal
from core.config import settings
from utils.get_logger import logger


router = APIRouter(prefix="/internal", tags=["Internal Services"])


def get_service_user(
    subject: "JwtAuthorizationCredentials" = Depends(access_security),
    db: Session = Depends(get_db),
) -> User:
    """Resolve user from service JWT only."""
    payload = subject.subject
    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: user_id missing",
        )
    if not is_internal_service_token_payload(payload):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service token required",
        )
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user


class InternalMessageAppendRequest(BaseModel):
    """Payload for appending a message to a session."""

    session_id: str = Field(..., min_length=1)
    user_question: str = Field(..., min_length=1)
    model_answer: str = Field(..., min_length=1)
    retrieval_content: Optional[Dict[str, Any]] = None
    source: Optional[str] = Field(default=None, description="Origin of the message.")
    trace_id: Optional[str] = Field(default=None, description="Optional trace id.")


class InternalSessionRewindRequest(BaseModel):
    """Payload for rewinding a session to a message prefix."""

    keep_messages: Optional[int] = Field(
        default=None,
        ge=0,
        description="Number of earliest messages to keep.",
    )
    before_message_id: Optional[str] = Field(
        default=None,
        description="Keep messages strictly before this message_id.",
    )


def _run_rolling_summary_update(session_id: str) -> None:
    """Update rolling summary using a fresh DB session."""
    db = SessionLocal()
    try:
        conversation_service = ConversationService(db)
        conversation_service.maybe_update_rolling_summary(session_id=session_id)
    except Exception as exc:
        logger.warning("Failed to update rolling summary: %s", exc)
    finally:
        db.close()


@router.get("/retrieve", response_model=List[RagChunk], summary="内部服务检索接口")
def internal_retrieve(
    q: str = Query(..., description="查询文本"),
    kb_id: int = Query(..., description="知识库 ID"),
    top_k: int = Query(settings.SM_RAG_TOPK, ge=1, le=50, description="返回数量"),
    focus_doc_ids: Optional[str] = Query(None, description="以逗号分隔的 document_id 列表"),
    provider: Optional[str] = Query(None, description="RAG provider override"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_service_user),
):
    """
    内部服务专用检索接口（无需 session_id）
    
    用于 Doc Studio 等内部服务直接基于 kb_id 进行检索
    不依赖 session，直接使用 global_only 模式
    """
    # 验证知识库归属
    try:
        kb = knowledgebase_service.get_kb_by_id(db=db, kb_id=kb_id, user_id=current_user.id)
    except Exception as e:
        logger.error(f"Knowledge base validation failed: {e}")
        raise HTTPException(status_code=404, detail="知识库不存在或无权访问")
    
    # 解析 focus_doc_ids
    focus_ids_list = None
    if focus_doc_ids:
        try:
            focus_ids_list = [int(x) for x in focus_doc_ids.split(",") if x.strip().isdigit()]
        except Exception:
            focus_ids_list = None
    
    # 执行检索（global_only 模式，不使用 session index）
    retriever = RAGRetriever()
    provider_name = resolve_provider(provider or getattr(kb, "rag_provider", None))
    try:
        results = retriever.retrieve(
            query=q,
            kb_id=kb_id,
            top_k=top_k,
            focus_doc_ids=focus_ids_list,
            session_index=None,
            index_mode="global_only",
            provider=provider_name,
        )
        
        logger.info(f"Internal retrieve: kb_id={kb_id}, query={q[:50]}..., results={len(results)}")
        
        # 转换为 RagChunk 格式
        out: List[RagChunk] = []
        for item in results:
            md = item.get("metadata") or {}
            md["rag_provider"] = provider_name
            out.append(
                RagChunk(
                    chunk_id=str(item.get("chunk_id", "")),
                    document_id=str(md.get("document_id", "")),
                    content=item.get("text", ""),
                    metadata=md,
                )
            )
        return out
    
    except Exception as e:
        logger.error(f"Internal retrieve failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"检索失败: {str(e)}")


@router.get("/history/{session_id}", summary="内部 STM 历史切片接口")
def internal_history(
    session_id: str,
    question: str = Query("", description="触发 STM 选择的查询文本"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_service_user),
):
    """Return STM-filtered history for service-side context injection.

    Args:
        session_id (str): Session identifier.
        question (str): Query text used to compute STM relevance.
        db (Session): Database session dependency.
        current_user (User): Authenticated user.

    Returns:
        dict: History slice with debug metadata.
    """
    session_service = SessionService(db)
    session_obj = session_service.get_session_by_id(session_id=session_id)
    if not session_obj or str(session_obj.user_id) != str(current_user.id):
        raise HTTPException(status_code=404, detail="Session 不存在或无权访问")

    conversation_service = ConversationService(db)
    history, debug, _ = conversation_service.build_history_slice(
        session_id=session_id,
        question=question or "",
    )

    return {
        "session_id": session_id,
        "question": question,
        "history": history,
        "debug": asdict(debug),
    }


@router.post("/messages", summary="内部追加会话消息")
def append_message(
    payload: InternalMessageAppendRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_service_user),
):
    """Append a message to a session for internal services.

    Args:
        payload (InternalMessageAppendRequest): Message payload.
        db (Session): Database session dependency.
        current_user (User): Authenticated user.

    Returns:
        dict: Created message id.
    """
    session_service = SessionService(db)
    session_obj = session_service.get_session_by_id(session_id=payload.session_id)
    if not session_obj or str(session_obj.user_id) != str(current_user.id):
        raise HTTPException(status_code=404, detail="Session 不存在或无权访问")

    retrieval_payload = dict(payload.retrieval_content or {})
    if payload.source:
        retrieval_payload["source"] = payload.source
    if payload.trace_id:
        retrieval_payload["trace_id"] = payload.trace_id

    retrieval_content = (
        json.dumps(retrieval_payload, ensure_ascii=False)
        if retrieval_payload
        else None
    )
    message = Message(
        session_id=payload.session_id,
        user_question=payload.user_question,
        model_answer=payload.model_answer,
        retrieval_content=retrieval_content,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    background_tasks.add_task(_run_rolling_summary_update, payload.session_id)
    return {"message_id": str(message.message_id)}


@router.get("/sessions/{session_id}/messages", summary="内部服务获取会话消息列表")
def internal_list_messages(
    session_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_service_user),
):
    """Doc Studio 等内部服务获取会话消息，用于加载对话历史。"""
    service = SessionManagementService(db=db, current_user=current_user)
    return service.list_messages(session_id=session_id, page=page, page_size=page_size)


@router.post("/sessions/{session_id}/rewind", summary="内部服务回卷会话消息")
def internal_rewind_messages(
    session_id: str,
    payload: InternalSessionRewindRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_service_user),
):
    """Rewind a session to the first N messages for branching flows."""
    logger.info(
        "Internal rewind request: session_id=%s user_id=%s keep_messages=%s before_message_id=%s",
        session_id,
        current_user.id,
        payload.keep_messages,
        payload.before_message_id,
    )
    service = SessionManagementService(db=db, current_user=current_user)
    if payload.before_message_id:
        result = service.rewind_messages(
            session_id=session_id,
            before_message_id=payload.before_message_id,
        )
        logger.info("Internal rewind finished: %s", result)
        return result
    if payload.keep_messages is None:
        raise HTTPException(status_code=400, detail="Either keep_messages or before_message_id is required")
    result = service.rewind_messages(
        session_id=session_id,
        keep_messages=payload.keep_messages,
    )
    logger.info("Internal rewind finished: %s", result)
    return result


@router.get("/context/{session_id}", summary="内部对话上下文接口")
def internal_context(
    session_id: str,
    question: str = Query("", description="触发 STM 选择的查询文本"),
    memory_limit: int = Query(10, ge=1, le=50, description="返回的记忆条目数量"),
    history_limit: int = Query(8, ge=1, le=50, description="历史片段返回条数"),
    memory_preview_limit: int = Query(8, ge=1, le=50, description="记忆摘要条数"),
    max_tokens: int = Query(settings.SM_CONTEXT_PACK_MAX_TOKENS, ge=256, le=8192, description="上下文最大 token 数"),
    max_chars: int = Query(settings.SM_CONTEXT_PACK_MAX_CHARS, ge=1000, le=20000, description="上下文最大字符数"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_service_user),
):
    """Return a unified context pack for internal services.

    Args:
        session_id (str): Session identifier.
        question (str): Query text used for STM selection.
        memory_limit (int): Max number of memory items.
        db (Session): Database session dependency.
        current_user (User): Authenticated user.

    Returns:
        dict: Context pack with history, memory, and formatted text.
    """
    session_service = SessionService(db)
    session_obj = session_service.get_session_by_id(session_id=session_id)
    if not session_obj or str(session_obj.user_id) != str(current_user.id):
        raise HTTPException(status_code=404, detail="Session 不存在或无权访问")

    conversation_service = ConversationService(db)
    pack = conversation_service.build_context_pack(
        session_id=session_id,
        user_id=current_user.id,
        question=question or "",
        memory_limit=memory_limit,
        history_limit=history_limit,
        memory_preview_limit=memory_preview_limit,
        max_text_chars=max_chars,
        max_context_tokens=max_tokens,
    )
    debug = pack.get("debug")
    return {
        "session_id": session_id,
        "question": question,
        "history": pack.get("history") or [],
        "debug": asdict(debug) if debug else {},
        "memory": {
            "items": pack.get("memory_items") or [],
            "count": len(pack.get("memory_items") or []),
        },
        "context_text": pack.get("context_text") or "",
        "rolling_summary": pack.get("rolling_summary"),
        "context_meta": pack.get("context_meta") or {},
    }


@router.get("/profile/{user_id}", summary="内部 LTM 画像接口")
def internal_profile(
    user_id: int,
    limit: int = Query(10, ge=1, le=50, description="返回的记忆条目数量"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_service_user),
):
    """Return LTM memory snippets for internal services.

    Args:
        user_id (int): User id to fetch memories for.
        limit (int): Max number of memory items.
        db (Session): Database session dependency.
        current_user (User): Authenticated user.

    Returns:
        dict: Memory entries with metadata.
    """
    if str(user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="无权访问该用户记忆")

    conversation_service = ConversationService(db)
    items = conversation_service.list_memory_profile(
        user_id=user_id,
        limit=limit,
    )

    return {
        "user_id": user_id,
        "items": items,
        "count": len(items),
    }

