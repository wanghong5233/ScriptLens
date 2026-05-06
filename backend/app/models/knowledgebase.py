from sqlalchemy import Column, Integer, String, ForeignKey, TIMESTAMP, Text, Boolean, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from models.base import Base

class KnowledgeBase(Base):
    """
    知识库模型，用于表示用户创建的独立知识库。
    """
    __tablename__ = 'knowledgebases'

    id = Column(Integer, primary_key=True, autoincrement=True, comment="知识库唯一ID")
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, comment="关联的用户ID")
    name = Column(String(255), nullable=False, comment="知识库名称")
    description = Column(Text, nullable=True, comment="知识库描述信息")
    rag_provider = Column(String(64), nullable=True, comment="RAG Provider（multi_stage/graph/multimodal_graph）")
    rag_config = Column(JSON, nullable=True, comment="RAG Provider 配置（JSON）")
    
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now(), comment="创建时间")
    updated_at = Column(TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now(), comment="最后更新时间")

    # 会话知识库标记（生命周期与 session 绑定；会话删除时一并清理）
    is_ephemeral = Column(Boolean, nullable=False, server_default='false')

    # 建立与User模型的关系
    # back_populates指定了在User模型中，哪个关系属性可以反向访问到这个KnowledgeBase模型
    user = relationship("User", back_populates="knowledge_bases")
    
    # 建立与Document模型的关系
    # a. cascade="all, delete-orphan": 级联操作。
    #    - all: 对KnowledgeBase的所有操作（如保存、删除）都会级联到其关联的Documents。
    #    - delete-orphan: 当一个Document从一个KnowledgeBase的documents列表中被移除时，这个Document记录将从数据库中被删除。
    # b. back_populates="knowledge_base": 指定了在Document模型中，可以通过knowledge_base属性反向访问这个KnowledgeBase实例。
    # c. lazy="selectin": 加载策略。
    #    - 当加载一个KnowledgeBase对象时，SQLAlchemy会发出第二条SQL查询，
    #      一次性加载该知识库关联的所有Document对象。这比默认的"select"（需要时逐个加载）或"joined"（在初始查询中用JOIN加载）更高效，
    #      避免了N+1查询问题。
    documents = relationship("Document", cascade="all, delete-orphan", back_populates="knowledge_base", lazy="selectin")

    def __repr__(self):
        return f"<KnowledgeBase(id={self.id}, name='{self.name}', user_id={self.user_id})>" 