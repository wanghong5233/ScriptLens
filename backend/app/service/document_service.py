from typing import Any, Dict, List, Optional, Set
from sqlalchemy.orm import Session
from sqlalchemy import or_
from models.document import Document
from models.knowledgebase import KnowledgeBase
from schemas.document import DocumentUpdate, DocumentCreate
from exceptions.base import ResourceNotFoundException, PermissionDeniedException, APIException
from core.config import settings
import os
from utils.get_logger import logger

def get_document_by_id(db: Session, doc_id: int, user_id: int, kb_id: int = None) -> Document:
    """
    通过ID获取文档，并校验用户权限。

    Args:
        db (Session): 数据库会话。
        doc_id (int): 文档ID。
        user_id (int): 当前用户ID。
        kb_id (int, optional): 文档所属的知识库ID，用于校验。

    Returns:
        Document: 找到的文档模型实例。
    
    Raises:
        ResourceNotFoundException: 如果文档未找到。
        PermissionDeniedException: 如果用户无权访问该文档。
    """
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise ResourceNotFoundException(f"Document with id {doc_id} not found.")

    # 增加校验：确保文档属于指定的知识库
    if kb_id is not None and doc.knowledge_base_id != kb_id:
        raise PermissionDeniedException("Document does not belong to the specified knowledge base.")
    
    # 校验用户是否有权访问该文档所属的知识库
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == doc.knowledge_base_id).first()
    if not kb or kb.user_id != user_id:
        raise PermissionDeniedException("You do not have permission to access this document.")
        
    return doc

def list_documents_by_kb_id(db: Session, kb_id: int, user_id: int) -> List[Document]:
    """
    获取指定知识库下的所有文档，并校验用户权限。

    Args:
        db (Session): 数据库会话。
        kb_id (int): 知识库ID。
        user_id (int): 当前用户ID。

    Returns:
        List[Document]: 文档模型实例列表。

    Raises:
        ResourceNotFoundException: 如果知识库未找到。
        PermissionDeniedException: 如果用户无权访问该知识库。
    """

    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb:
        raise ResourceNotFoundException(f"KnowledgeBase with id {kb_id} not found.")
    if kb.user_id != user_id:
        raise PermissionDeniedException("You do not have permission to access this knowledge base.")
        
    return db.query(Document).filter(Document.knowledge_base_id == kb_id).all()

def update_document(db: Session, doc_id: int, doc_update: DocumentUpdate, user_id: int, kb_id: int) -> Document:
    """
    更新文档元数据。

    Args:
        db (Session): 数据库会话。
        doc_id (int): 要更新的文档ID。
        doc_update (DocumentUpdate): 包含更新数据的Pydantic模型。
        user_id (int): 当前用户ID。
        kb_id (int): 文档所属的知识库ID，用于校验。

    Returns:
        Document: 更新后的文档模型实例。
    """
    doc_to_update = get_document_by_id(db, doc_id, user_id, kb_id=kb_id)
    
    update_data = doc_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(doc_to_update, key, value)
        
    db.commit()
    db.refresh(doc_to_update)
    return doc_to_update

def delete_document(db: Session, doc_id: int, user_id: int, kb_id: int) -> Document:
    """
    从知识库中删除一个文档。
    
    注意：此函数目前只处理数据库层面的删除。
    后续需要扩展，以同步删除向量存储和文件存储中的数据。

    Args:
        db (Session): 数据库会话。
        doc_id (int): 要删除的文档ID。
        user_id (int): 当前用户ID。
        kb_id (int): 文档所属的知识库ID，用于校验。

    Returns:
        Document: 被删除的文档模型实例。
    """
    logger.info(f"DeleteDocument invoked: kb_id={kb_id}, doc_id={doc_id}, user_id={user_id}")
    doc_to_delete = get_document_by_id(db, doc_id, user_id, kb_id=kb_id)
    logger.info(f"Resolved document: id={doc_to_delete.id}, kb_id={doc_to_delete.knowledge_base_id}")
    
    # 先删除本地文件（如果存在），失败容错，不阻塞整体删除
    try:
        logger.info(
            "Attempting local file removal for doc_id=%s: path=%s",
            doc_to_delete.id,
            doc_to_delete.local_pdf_path,
        )
        if doc_to_delete.local_pdf_path and os.path.exists(doc_to_delete.local_pdf_path):
            os.remove(doc_to_delete.local_pdf_path)
    except Exception as exc:
        logger.warning("Failed to remove local file for doc_id=%s: %s", doc_to_delete.id, exc)

    _delete_document_chunks_from_vector_store(kb_id=kb_id, document_id=doc_to_delete.id)

    # 知识图谱模块已在 ScriptLens 裁剪（短剧业务不依赖 KnowledgeGraph）
    db.delete(doc_to_delete)
    db.commit()
    
    return doc_to_delete


def _ensure_kb_access(db: Session, kb_id: int, user_id: int) -> KnowledgeBase:
    """
    校验知识库存在且属于当前用户，返回 KB。
    """
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb:
        raise ResourceNotFoundException(f"KnowledgeBase with id {kb_id} not found.")
    if kb.user_id != user_id:
        raise PermissionDeniedException("You do not have permission to access this knowledge base.")
    return kb


def _find_duplicate_document(
    db: Session,
    kb_id: int,
    semantic_scholar_id: Optional[str],
    doi: Optional[str],
    file_hash: Optional[str]
) -> Optional[Document]:
    """
    在同一知识库内基于 semantic_scholar_id / doi / file_hash 查重。
    只要任意一个非空字段匹配，即视为重复。
    """
    query = db.query(Document).filter(Document.knowledge_base_id == kb_id)

    # 按优先级尝试匹配
    if semantic_scholar_id:
        existing = query.filter(Document.semantic_scholar_id == semantic_scholar_id).first()
        if existing:
            return existing
    if doi:
        existing = query.filter(Document.doi == doi).first()
        if existing:
            return existing
    if file_hash:
        existing = query.filter(Document.file_hash == file_hash).first()
        if existing:
            return existing
    return None


def find_document_by_file_hash(db: Session, kb_id: int, file_hash: str) -> Optional[Document]:
    """
    在指定知识库中通过文件哈希查找文档。
    """
    if not file_hash:
        return None
    return (
        db.query(Document)
        .filter(
            Document.knowledge_base_id == kb_id,
            Document.file_hash == file_hash,
        )
        .first()
    )

def _resolve_es_targets(es_conn: Any, prefix: str) -> Set[str]:
    from elasticsearch import NotFoundError

    targets: Set[str] = set()
    if settings.ES_DEFAULT_INDEX:
        targets.add(settings.ES_DEFAULT_INDEX)
    targets.add("default")
    targets.add(f"{prefix}*")
    try:
        alias_info: Dict[str, Dict] = es_conn.es.indices.get_alias(index=f"{prefix}*")
        for index_name, meta in alias_info.items():
            if index_name:
                targets.add(index_name)
            for alias in (meta.get("aliases") or {}).keys():
                if alias:
                    targets.add(alias)
    except NotFoundError:
        pass
    except Exception as exc:
        logger.warning(f"Failed to resolve ES aliases for prefix '{prefix}': {exc}")
    return targets


def _delete_document_chunks_from_vector_store(*, kb_id: int, document_id: int) -> None:
    vector_store = str(getattr(settings, "SM_VECTOR_STORE", "pgvector") or "pgvector").strip().lower()
    if vector_store == "pgvector":
        _delete_document_chunks_from_pgvector(kb_id=kb_id, document_id=document_id)
        return
    _delete_document_chunks_from_es(kb_id=kb_id, document_id=document_id)


def _delete_document_chunks_from_pgvector(*, kb_id: int, document_id: int) -> None:
    logger.info("Start pgvector deletion phase for doc_id=%s, kb_id=%s", document_id, kb_id)
    try:
        from service.core.ingestion.pgvector_writer import PgVectorChunkWriter

        writer = PgVectorChunkWriter()
        removed = writer.delete_document_chunks(kb_id=kb_id, document_id=document_id)
        residual = writer.count_document_chunks(kb_id=kb_id, document_id=document_id)
        logger.info(
            "pgvector deletion summary for doc_id=%s, kb_id=%s: chunks_removed=%s",
            document_id,
            kb_id,
            removed,
        )
        if residual > 0:
            raise APIException(
                f"Detected {residual} residual pgvector chunks for doc_id={document_id}. "
                "Deletion aborted to avoid inconsistent state."
            )
    except Exception as exc:
        logger.exception("An error occurred during pgvector deletion for doc_id=%s. Error: %s", document_id, exc)
        raise


def _delete_document_chunks_from_es(*, kb_id: int, document_id: int) -> None:
    logger.info("Start ES deletion phase for doc_id=%s, kb_id=%s", document_id, kb_id)
    try:
        from service.core.rag.utils.es_conn import ESConnection

        es = ESConnection()
        es_prefix = settings.ES_DEFAULT_INDEX.split("_")[0]
        target_indices = _resolve_es_targets(es, es_prefix)
        total_removed = 0
        residual_indices: Dict[str, int] = {}
        for idx in sorted(target_indices):
            removed = _delete_chunks_from_index(
                es_conn=es,
                index_name=idx,
                kb_id=str(kb_id),
                doc_id=str(document_id),
            )
            total_removed += removed
            residual = _count_chunks_in_index(
                es_conn=es,
                index_name=idx,
                kb_id=str(kb_id),
                doc_id=str(document_id),
            )
            if residual > 0:
                residual_indices[idx] = residual
        logger.info(
            "ES deletion summary for doc_id=%s, kb_id=%s: indices_checked=%s, chunks_removed=%s",
            document_id,
            kb_id,
            len(target_indices),
            total_removed,
        )
        if residual_indices:
            raise APIException(
                f"Detected {sum(residual_indices.values())} residual chunks for doc_id={document_id}: "
                f"{residual_indices}. Deletion aborted to avoid inconsistent state."
            )
    except Exception as exc:
        logger.exception("An error occurred during ES deletion for doc_id=%s. Error: %s", document_id, exc)
        raise


def _delete_chunks_from_index(es_conn: Any, index_name: str, kb_id: str, doc_id: str) -> int:
    """
    多次调用 delete_by_query，直到删除数为 0，确保索引彻底清理。
    """
    total_removed = 0
    consecutive_zero = 0
    max_attempts = 5
    for _ in range(max_attempts):
        removed = es_conn.delete({"document_id": doc_id}, indexName=index_name, knowledgebaseId=kb_id)
        if removed <= 0:
            consecutive_zero += 1
            if consecutive_zero >= 2:
                break
            continue
        total_removed += removed
        consecutive_zero = 0
    if total_removed > 0:
        logger.info(f"Removed {total_removed} chunks from index '{index_name}' for doc_id={doc_id}.")
    return total_removed


def _count_chunks_in_index(es_conn: Any, index_name: str, kb_id: str, doc_id: str) -> int:
    from elasticsearch import NotFoundError

    try:
        body = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"kb_id": kb_id}},
                        {"term": {"document_id": doc_id}},
                    ]
                }
            }
        }
        res = es_conn.es.count(index=index_name, body=body)
        return int(res.get("count", 0))
    except NotFoundError:
        return 0
    except Exception as exc:
        logger.warning(
            "Failed to count residual chunks in index '%s' for doc_id=%s: %s",
            index_name,
            doc_id,
            exc,
        )
        return 0


def create_documents_bulk_for_kb(
    db: Session,
    kb_id: int,
    user_id: int,
    documents: List[DocumentCreate]
) -> List[Document]:
    """
    批量创建文档（带去重），并返回成功创建的文档列表。

    - 先校验用户对知识库的访问权限
    - 对每个文档基于 semantic_scholar_id / doi / file_hash 在 KB 内查重
    - 非重复则创建，重复则跳过
    """
    _ensure_kb_access(db, kb_id, user_id)

    created: List[Document] = []

    for doc in documents:
        duplicate = _find_duplicate_document(
            db=db,
            kb_id=kb_id,
            semantic_scholar_id=doc.semantic_scholar_id,
            doi=doc.doi,
            file_hash=doc.file_hash,
        )
        if duplicate:
            # 跳过重复
            continue

        new_doc = Document(
            knowledge_base_id=kb_id,
            title=doc.title,
            authors=doc.authors,
            abstract=doc.abstract,
            publication_year=doc.publication_year,
            journal_or_conference=doc.journal_or_conference,
            keywords=doc.keywords,
            citation_count=doc.citation_count,
            fields_of_study=doc.fields_of_study,
            doi=doc.doi,
            semantic_scholar_id=doc.semantic_scholar_id,
            source_url=doc.source_url,
            local_pdf_path=doc.local_pdf_path,
            file_hash=doc.file_hash,
            ingestion_source=doc.ingestion_source.value if hasattr(doc.ingestion_source, "value") else doc.ingestion_source,
        )

        db.add(new_doc)
        db.flush()  # 先拿到自增ID
        created.append(new_doc)

    if created:
        db.commit()
        for d in created:
            db.refresh(d)

    return created


def find_existing_documents_for_payload(
    db: Session,
    kb_id: int,
    documents: List[DocumentCreate]
) -> List[Document]:
    """
    根据传入的 DocumentCreate 列表，在指定 KB 内查找已存在的文档。
    仅返回那些 semantic_scholar_id 或 DOI 匹配的记录。
    """
    semantic_ids: Set[str] = set(
        d.semantic_scholar_id for d in documents if getattr(d, "semantic_scholar_id", None)
    )
    dois: Set[str] = set(
        d.doi for d in documents if getattr(d, "doi", None)
    )

    if not semantic_ids and not dois:
        return []

    q = db.query(Document).filter(Document.knowledge_base_id == kb_id)
    conditions = []
    if semantic_ids:
        conditions.append(Document.semantic_scholar_id.in_(list(semantic_ids)))
    if dois:
        conditions.append(Document.doi.in_(list(dois)))

    return q.filter(or_(*conditions)).all()