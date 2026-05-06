from sqlalchemy import (
    Column, Integer, String, Text, ForeignKey, TIMESTAMP, JSON, Index, DateTime, func, UniqueConstraint
)
from sqlalchemy.orm import relationship
import enum

from models.base import Base

class DocumentIngestionSource(enum.Enum):
    ONLINE_IMPORT = "online_import"
    LOCAL_UPLOAD = "local_upload"


class DocumentProcessingStatus(str, enum.Enum):
    """Lifecycle of a document in the ingestion pipeline.

    Single source of truth for "is this doc retrievable by RAG?".
    The KB list, the job records and the retry endpoint all read from this.

    Transitions:
        pending  -> parsing  -> ready     (happy path)
        pending  -> parsing  -> failed    (parse/chunk/embed/index error)
        pending  -> failed                (download error before parsing starts)
        failed   -> parsing               (user-triggered retry)
    """

    PENDING = "pending"
    PARSING = "parsing"
    READY = "ready"
    FAILED = "failed"


class Document(Base):
    """
    文档模型，用于存储导入到特定知识库中的每篇论文的详细元数据。
    """
    __tablename__ = 'documents'

    id = Column(Integer, primary_key=True, autoincrement=True, comment="文档唯一ID")
    knowledge_base_id = Column(Integer, ForeignKey('knowledgebases.id', ondelete='CASCADE'), nullable=False, comment="关联的知识库ID")
    
    # 核心元数据
    title = Column(Text, nullable=False, comment="论文标题")
    authors = Column(JSON, nullable=True, comment="作者列表，存储为JSON数组")
    abstract = Column(Text, nullable=True, comment="论文摘要")
    publication_year = Column(Integer, nullable=True, comment="发表年份")
    journal_or_conference = Column(String(255), nullable=True, comment="期刊或会议名称")
    
    # 新增的丰富元数据
    keywords = Column(JSON, nullable=True, comment="论文关键词列表")
    citation_count = Column(Integer, nullable=True, comment="被引次数")
    fields_of_study = Column(JSON, nullable=True, comment="研究领域列表")

    # 唯一标识符，用于去重
    doi = Column(String(255), nullable=True, comment="Digital Object Identifier")
    semantic_scholar_id = Column(String(255), nullable=True, comment="Semantic Scholar的唯一ID")
    
    # 文件相关信息
    source_url = Column(Text, nullable=True, comment="文献来源URL")
    local_pdf_path = Column(String(1024), nullable=True, comment="本地PDF文件存储路径")
    file_hash = Column(String(255), nullable=True, comment="本地文件的SHA256哈希值")
    ingestion_source = Column(String(50), nullable=False, default=DocumentIngestionSource.LOCAL_UPLOAD.value, comment="文档来源")
    structure_metadata = Column(JSON, nullable=True, comment="结构化解析元数据快照")

    # 处理生命周期 - 由 ingestion / parse / index handler 流转，是 RAG 是否可用的唯一真理
    processing_status = Column(
        String(32),
        nullable=False,
        default=DocumentProcessingStatus.PENDING.value,
        server_default=DocumentProcessingStatus.PENDING.value,
        comment="pending | parsing | ready | failed",
    )
    chunk_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="rag_chunks 中的实际记录数（缓存字段，由 indexer 写入）",
    )
    failure_stage = Column(
        String(32),
        nullable=True,
        comment="download | parse | chunk | embed | index | legacy",
    )
    failure_reason = Column(Text, nullable=True, comment="失败时的可读原因（供 UI 展示）")
    last_processed_at = Column(TIMESTAMP, nullable=True, comment="上一次进入 ready/failed 终态的时间")

    # 时间戳
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now(), comment="创建时间")
    updated_at = Column(TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now(), comment="最后更新时间")

    # 建立与KnowledgeBase模型的关系
    knowledge_base = relationship("KnowledgeBase", back_populates="documents")

    __table_args__ = (
        # 在同一个知识库内，semantic_scholar_id 应该是唯一的
        Index('uq_kb_semantic_id', 'knowledge_base_id', 'semantic_scholar_id', unique=True),
        # 在同一个知识库内，doi 也应该是唯一的
        Index('uq_kb_doi', 'knowledge_base_id', 'doi', unique=True),
        # 在同一个知识库内，file_hash 也应该是唯一的
        Index('uq_kb_file_hash', 'knowledge_base_id', 'file_hash', unique=True),
    )

    def __repr__(self):
        return f"<Document(id={self.id}, title='{self.title[:30]}...', kb_id={self.knowledge_base_id})>"
