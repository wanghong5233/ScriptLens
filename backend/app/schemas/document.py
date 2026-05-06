from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Dict, Any

from models.document import DocumentIngestionSource

class DocumentBase(BaseModel):
    """
    文档的基础 Pydantic 模型，包含所有文档共有的核心元数据字段。
    """
    title: str
    authors: Optional[List[str]] = None
    abstract: Optional[str] = None
    publication_year: Optional[int] = None
    journal_or_conference: Optional[str] = None
    keywords: Optional[List[str]] = None
    citation_count: Optional[int] = None
    fields_of_study: Optional[List[str]] = None
    doi: Optional[str] = None
    semantic_scholar_id: Optional[str] = None
    source_url: Optional[str] = None
    local_pdf_path: Optional[str] = None
    file_hash: Optional[str] = None
    ingestion_source: DocumentIngestionSource

    class Config:
        from_attributes = True

class DocumentCreate(DocumentBase):
    """
    用于在特定知识库中创建新文档的 Pydantic 模型。
    它只定义了API请求体中应该包含的字段。
    knowledge_base_id 将从URL路径参数中获取，而不是在请求体中。
    """
    highLight: bool | None = None
    quality_source: Optional[str] = None
    quality_rank: Optional[str] = None
    quality_label: Optional[str] = None
    quality_score: Optional[int] = None
    # All matched venue ranks (CCF + JCR may co-occur). Each item:
    # ``{"source": "CCF"|"JCR", "rank": "A"/"Q1"/..., "label": "CCF-A"/"JCR-Q1"}``.
    quality_labels: Optional[List[dict]] = None

class DocumentUpdate(BaseModel):
    """
    用于更新文档的 Pydantic 模型。
    所有字段都设为可选，以便可以只更新部分字段。
    """
    title: Optional[str] = None
    authors: Optional[List[str]] = None
    abstract: Optional[str] = None
    publication_year: Optional[int] = None
    journal_or_conference: Optional[str] = None
    keywords: Optional[List[str]] = None
    citation_count: Optional[int] = None
    fields_of_study: Optional[List[str]] = None
    doi: Optional[str] = None
    semantic_scholar_id: Optional[str] = None
    source_url: Optional[str] = None
    local_pdf_path: Optional[str] = None

class DocumentInDB(DocumentBase):
    """
    作为API响应返回给客户端的文档模型。
    它继承自 DocumentBase，并增加了数据库自动生成的字段。
    """
    id: int
    knowledge_base_id: int
    created_at: datetime
    updated_at: datetime
    structure_metadata: Optional[Dict[str, Any]] = None

    # Lifecycle fields - drive UI status badge / retry button / failure tooltip.
    processing_status: str = "pending"
    chunk_count: int = 0
    failure_stage: Optional[str] = None
    failure_reason: Optional[str] = None
    last_processed_at: Optional[datetime] = None

    class Config:
        from_attributes = True

# 尽管当前 DocumentInDB 没有向前引用，但添加 rebuild 是一个好习惯
# 它可以确保未来如果添加了对 KnowledgeBaseInDB 的引用，也能正确解析
DocumentInDB.model_rebuild()


class CriticalQuestionsResponse(BaseModel):
    """批判性问题生成响应。"""
    questions: List[str]
    citations: List[Dict[str, Any]] = []
    debug: Dict[str, Any] = {}


class DocumentParseBlock(BaseModel):
    """单个解析块的结构化表示。"""

    index: int = Field(..., description="块在解析结果中的顺序，从 1 开始。")
    text: str = Field("", description="块的文本内容。")
    element_type: Optional[str] = Field(
        default=None, description="块的类型，例如 paragraph/table_json/equation_latex。"
    )
    page: Optional[int] = Field(default=None, description="所属页码（从 1 开始）。")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="原始解析 metadata。")


class DocumentParseStats(BaseModel):
    """解析统计信息。"""

    total_blocks: int = Field(..., description="解析返回的块总数。")
    nonempty_blocks: int = Field(..., description="文本非空的块数量。")
    total_chars: int = Field(..., description="所有块文本长度之和。")
    element_types: Dict[str, int] = Field(default_factory=dict, description="不同 element_type 的数量统计。")
    parser_engines: Dict[str, int] = Field(
        default_factory=dict, description="不同 parser_engine 的数量统计。"
    )


class DocumentParseStage(BaseModel):
    """单个解析阶段（原始解析/分块/入库）的详情。"""

    key: str = Field(..., description="阶段唯一标识，如 parser/chunker/indexed。")
    title: str = Field(..., description="阶段名称，前端展示用。")
    description: Optional[str] = Field(default=None, description="阶段说明。")
    stats: DocumentParseStats
    blocks: List[DocumentParseBlock]


class DocumentParsePreviewResponse(BaseModel):
    """文档解析预览响应。"""

    document_id: int
    knowledge_base_id: int
    filename: Optional[str] = None
    parser_order: List[str] = Field(default_factory=list, description="解析器尝试顺序。")
    stages: List[DocumentParseStage] = Field(default_factory=list, description="各阶段解析/分块/入库的内容。")
    stats: DocumentParseStats
    blocks: List[DocumentParseBlock]

