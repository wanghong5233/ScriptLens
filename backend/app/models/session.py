from sqlalchemy import Column, String, TIMESTAMP, Integer, ForeignKey, Text, Boolean
from sqlalchemy.sql import func
from models.base import Base

class Session(Base):
    """
    SQLAlchemy ORM 模型，用于映射数据库中的 `sessions` 表。

    该表存储了用户的聊天会话信息，每个会话都是一个独立的聊天上下文。
    """
    __tablename__ = 'sessions'
    
    # session_id: 会话的唯一标识符，作为主键。
    # - String(16): 数据类型为字符串，长度16。
    # - primary_key=True: 将此列设置为主键。
    session_id = Column(String(16), primary_key=True)
    
    # session_name: 会话的名称，用于向用户展示。
    # - String(255): 数据类型为字符串，最大长度255。
    # - nullable=False: 会话名称不能为空。
    session_name = Column(String(255), nullable=False)
    
    # user_id: 该会话所属用户的ID（暂保留为字符串以兼容现有数据）。
    user_id = Column(String(255), nullable=False)

    # surface: 会话所属产品面（用于会话宇宙隔离）。
    # - deep_chat: 主站对话宇宙（含 deep_research / idea_gen 等能力）
    # - doc_studio: Doc Studio 编辑宇宙
    surface = Column(String(32), nullable=False, server_default="deep_chat")

    # knowledge_base_id: 该会话绑定的 Session KB ID（历史数据可能为空，运行时自动补齐）。
    knowledge_base_id = Column(Integer, ForeignKey('knowledgebases.id', ondelete='SET NULL'), nullable=True)
    
    # created_at: 会话记录的创建时间戳。
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())
    
    # updated_at: 会话记录的最后更新时间戳。
    updated_at = Column(TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()) 

    # 会话级默认参数（JSON 字符串），用于保存检索/生成等默认设置
    defaults_json = Column(Text, nullable=True)
    # 滚动摘要，保存多轮历史的压缩表示（可选）
    rolling_summary = Column(Text, nullable=True)
    # 记忆引导状态
    memory_guide_fail_count = Column(Integer, nullable=False, default=0)
    memory_guide_disabled = Column(Boolean, nullable=False, default=False)
    # 会话临时上下文（JSON 字符串），用于存储直接上传的文件内容等
    context_json = Column(Text, nullable=True)