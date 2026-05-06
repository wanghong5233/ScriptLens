from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

class Chunk(BaseModel):
    """
    表示一个文本块（Chunk）的数据模型。
    这是RAG流程中进行嵌入和检索的基本单位。
    """
    chunk_id: str = Field(..., description="文本块的唯一标识符")
    document_id: str = Field(..., description="所属文档的ID")
    content: str = Field(..., description="文本块的原始内容")
    
    # 预留的元数据字段，为未来的高级功能（如多模态、答案溯源）做准备
    metadata: Dict[str, Any] = Field(default_factory=dict, description="包含来源页码、类型（文本/表格/图片描述）等信息的元数据")
    
    # 可选的嵌入向量
    embedding: Optional[List[float]] = Field(None, description="文本块的嵌入向量")

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "chunk_id": "doc1_chunk_3",
                "document_id": "arxiv_2401.1001",
                "content": "Language models are becoming increasingly powerful...",
                "metadata": {
                    "page_number": 5,
                    "type": "text",
                    "bounding_box": [100, 200, 300, 400] # 示例：PDF中的坐标
                }
            }
        }

class Document(BaseModel):
    """
    表示一篇完整文档的数据模型。
    """
    document_id: str = Field(..., description="文档的唯一标识符")
    content: str = Field(..., description="文档的全部原始内容（可选，可能很大）")
    
    # 文档的元数据，如标题、作者、来源URL等
    metadata: Dict[str, Any] = Field(default_factory=dict, description="包含标题、作者、发布日期等信息的元数据")

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "document_id": "arxiv_2401.1001",
                "content": "Abstract: Language models...",
                "metadata": {
                    "title": "The Rise of LLMs",
                    "authors": ["John Doe"],
                    "source_url": "https://arxiv.org/abs/2401.1001"
                }
            }
        }
