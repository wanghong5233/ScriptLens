from fastapi import APIRouter, Depends, Security, HTTPException, status, Query
from sqlalchemy.orm import Session
from utils.database import get_db
from schemas.message import FilestResponse, SessionListResponse
from fastapi_jwt import JwtAuthorizationCredentials
from service.auth import access_security
from typing import List, Optional, Literal

from service.core.conversation.history_service import HistoryService

router = APIRouter()

############################
#   获取文档列表
############################

@router.get("/get_files", response_model=List[FilestResponse])
async def get_documents_by_user_id(
    credentials: JwtAuthorizationCredentials = Security(access_security),
    db: Session = Depends(get_db)
):
    """
    获取当前认证用户上传的所有文档列表。

    通过验证用户的JWT令牌来识别用户身份，然后从数据库中查询该用户
    关联的所有知识库文档记录。

    - **认证**: 需要提供有效的Bearer Token。
    - **返回**: 一个包含文档信息的对象列表，如果用户没有上传过文档则返回空列表。
    """
    service = HistoryService(db=db, credentials=credentials)
    return service.get_documents_by_user_id()

############################
#   删除文档
############################

@router.delete("/delete_file/{file_name}")
async def delete_document_endpoint(
    file_name: str,
    credentials: JwtAuthorizationCredentials = Security(access_security),
    db: Session = Depends(get_db)
):
    """
    【已废弃】删除指定名称的文档。
    
    此接口已被新的、基于文档ID的删除接口取代，不再维护。
    调用此接口将直接返回 410 Gone 状态。
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="This API is deprecated. Please use DELETE /api/knowledgebases/{kb_id}/documents/{doc_id} instead."
    )

@router.get("/get_messages")
async def get_messages_by_session_id(
    session_id: str,
    credentials: JwtAuthorizationCredentials = Security(access_security),
    db: Session = Depends(get_db)
):
    """
    根据会话ID获取该会话下的所有历史消息。

    查询与指定`session_id`关联的所有聊天记录，包括用户提问、模型回答等信息。

    - **查询参数**: `session_id` (str) - 目标会话的唯一标识符。
    - **认证**: 需要提供有效的Bearer Token。
    - **返回**: 一个包含该会话所有消息对象的列表。
    """
    service = HistoryService(db=db, credentials=credentials)
    return service.get_messages_by_session_id(session_id=session_id)
    
@router.get("/get_sessions", response_model=SessionListResponse)
async def get_sessions_by_user_id(
    surface: Optional[Literal["deep_chat", "doc_studio"]] = Query(
        default="deep_chat",
        description="按会话所属产品面过滤",
    ),
    credentials: JwtAuthorizationCredentials = Security(access_security),
    db: Session = Depends(get_db)
):
    """
    获取当前认证用户的所有历史会话列表。

    通过用户认证信息，查询并返回该用户创建的所有对话会话。

    - **认证**: 需要提供有效的Bearer Token。
    - **返回**: 包含用户ID和其所有会话列表的对象。
    """
    service = HistoryService(db=db, credentials=credentials)
    return service.get_sessions_by_user_id(surface=surface)