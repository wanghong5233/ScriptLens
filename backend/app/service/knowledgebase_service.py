from typing import List, Optional
from sqlalchemy.orm import Session
from models.knowledgebase import KnowledgeBase
from models.session import Session as SessionModel
from models.user import User
from schemas.knowledge_base import KnowledgeBaseCreate, KnowledgeBaseUpdate
from service.core.rag.providers.registry import resolve_provider
from exceptions.base import ResourceNotFoundException, PermissionDeniedException
from service import document_service
from utils.get_logger import logger
from datetime import datetime, timedelta


def create_kb_for_user(db: Session, kb_create: KnowledgeBaseCreate, user_id: int) -> KnowledgeBase:
    """
    为指定用户创建一个新的知识库。

    Args:
        db (Session): 数据库会话。
        kb_create (KnowledgeBaseCreate): 包含新知识库信息的Pydantic模型。
        user_id (int): 所属用户的ID。

    Returns:
        KnowledgeBase: 新创建的知识库ORM对象。
    """
    data = kb_create.model_dump()
    data["rag_provider"] = resolve_provider(data.get("rag_provider"))
    db_kb = KnowledgeBase(**data, user_id=user_id)
    db.add(db_kb)
    db.commit()
    db.refresh(db_kb)
    return db_kb

def get_kb_by_id(db: Session, kb_id: int, user_id: int) -> Optional[KnowledgeBase]:
    """
    根据ID获取单个知识库，并校验所有权。

    Args:
        db (Session): 数据库会话。
        kb_id (int): 知识库的ID。
        user_id (int): 当前请求用户的ID。

    Returns:
        Optional[KnowledgeBase]: 找到的知识库ORM对象。

    Raises:
        ResourceNotFoundException: 如果知识库不存在。
        PermissionDeniedException: 如果用户不是该知识库的所有者。
    """
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb:
        raise ResourceNotFoundException(f"KnowledgeBase with id {kb_id} not found.")
    if kb.user_id != user_id:
        raise PermissionDeniedException("You do not have permission to access this knowledge base.")
    return kb

def list_kbs_by_user_id(db: Session, user_id: int) -> List[KnowledgeBase]:
    """
    获取指定用户的所有知识库列表。
    """
    return db.query(KnowledgeBase).filter(KnowledgeBase.user_id == user_id).all()


def get_rag_provider(db: Session, kb_id: int, user_id: int) -> str:
    """Resolve the effective RAG provider for a knowledge base."""
    kb = get_kb_by_id(db, kb_id, user_id)
    return resolve_provider(getattr(kb, "rag_provider", None))

def list_ephemeral_kbs_older_than(db: Session, user_id: int, older_than_hours: int) -> List[KnowledgeBase]:
    cutoff = datetime.utcnow() - timedelta(hours=older_than_hours)
    bound_session_kb_query = db.query(SessionModel.knowledge_base_id).filter(
        SessionModel.knowledge_base_id.isnot(None)
    )
    return (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.user_id == user_id,
            KnowledgeBase.is_ephemeral == True,  # noqa: E712
            KnowledgeBase.created_at < cutoff,
            ~KnowledgeBase.id.in_(bound_session_kb_query),
        )
        .all()
    )

def cleanup_ephemeral_kbs(db: Session, user_id: int, older_than_hours: int) -> int:
    victims = list_ephemeral_kbs_older_than(db, user_id, older_than_hours)
    count = 0
    for kb in victims:
        try:
            delete_kb(db, kb_id=kb.id, user_id=user_id)
            count += 1
        except Exception as e:
            logger.error(f"清理会话知识库 {kb.id} 失败: {e}")
    return count

def update_kb(db: Session, kb_id: int, kb_update: KnowledgeBaseUpdate, user_id: int) -> Optional[KnowledgeBase]:
    """
    更新一个知识库的名称或描述，并校验所有权。
    """
    kb = get_kb_by_id(db, kb_id, user_id)  # 复用带有权限检查的查询函数
    
    update_data = kb_update.model_dump(exclude_unset=True)
    if "rag_provider" in update_data:
        update_data["rag_provider"] = resolve_provider(update_data.get("rag_provider"))
    for key, value in update_data.items():
        setattr(kb, key, value)
    
    db.commit()
    db.refresh(kb)
    return kb

def delete_kb(db: Session, kb_id: int, user_id: int) -> KnowledgeBase:
    """
    删除一个知识库，并校验所有权。
    此操作会级联删除所有关联的文档、本地文件和向量索引。
    """
    kb = get_kb_by_id(db, kb_id, user_id)

    # Note: We must operate on a copy of the list, as the original list
    # will be modified during the loop due to SQLAlchemy session changes.
    documents_to_delete = list(kb.documents)
    
    logger.info(f"开始删除知识库 '{kb.name}' (ID: {kb.id})，将级联删除 {len(documents_to_delete)} 个文档。")

    # Sequentially call document_service to delete each document and its associated resources
    for doc in documents_to_delete:
        try:
            # delete_document handles its own transaction and removal of file/ES data
            document_service.delete_document(db=db, doc_id=doc.id, kb_id=kb_id, user_id=user_id)
        except Exception as e:
            # Log the error but continue trying to delete other documents
            logger.error(f"删除文档ID {doc.id} (知识库ID: {kb_id}) 时出错: {e}")
            
    # Finally, delete the knowledge base itself
    logger.info(f"所有关联文档处理完毕，正在删除知识库 '{kb.name}' (ID: {kb.id}) 本身。")
    db.delete(kb)
    db.commit()
    logger.info(f"知识库 '{kb.name}' (ID: {kb.id}) 已成功删除。")
    
    return kb
