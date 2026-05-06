from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Dict, Any

# 移除临时的、不完整的类声明
# class DocumentInDB:
#     pass

class KnowledgeBaseBase(BaseModel):
    """
    知识库的基础 Pydantic 模型，包含所有知识库共有的字段。
    """
    name: str
    description: Optional[str] = None
    rag_provider: Optional[str] = None
    rag_config: Optional[Dict[str, Any]] = None

class KnowledgeBaseCreate(KnowledgeBaseBase):
    """
    用于创建新知识库的 Pydantic 模型。
    在基础模型之上，允许可选的 is_ephemeral 标记。
    """
    is_ephemeral: Optional[bool] = False

class KnowledgeBaseUpdate(KnowledgeBaseBase):
    """
    用于更新知识库的 Pydantic 模型。
    所有字段都设为可选，以便可以只更新部分字段。
    """
    name: Optional[str] = None
    description: Optional[str] = None
    rag_provider: Optional[str] = None
    rag_config: Optional[Dict[str, Any]] = None


class KnowledgeBaseInDBBase(KnowledgeBaseBase):
    """
    从数据库读取的知识库数据的基础模型，包含了数据库自动生成的字段。
    """
    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime
    is_ephemeral: bool = False

    class Config:
        # orm_mode = True 在 Pydantic V2 中已废弃，改用 from_attributes = True
        # 这个配置告诉 Pydantic 模型可以直接从 SQLAlchemy 的 ORM 对象实例中读取数据并进行映射。
        from_attributes = True

# 用于API响应的知识库模型（不包含关联的文档）
class KnowledgeBaseInDB(KnowledgeBaseInDBBase):
    """
    作为API响应返回给客户端的知识库模型。
    这是一个简洁的版本，不包含其下关联的所有文档，适用于列表展示等场景。
    """
    pass

# 用于API响应的知识库模型（包含关联的文档）
class KnowledgeBaseWithDocuments(KnowledgeBaseInDBBase):
    """
    一个更详细的知识库模型，作为API响应返回，其中包含了关联的文档列表。
    适用于获取单个知识库详情的场景。
    """
    documents: List['DocumentInDB'] = []

# 在所有类定义完成后，更新向前声明的引用
# 这是一种常见的技术，用于解决 Python 中两个类相互引用的问题
from .document import DocumentInDB
KnowledgeBaseWithDocuments.model_rebuild()
