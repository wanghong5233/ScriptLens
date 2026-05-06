from sqlalchemy import Column, String, Text, TIMESTAMP, Integer, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from models.base import Base

class Message(Base):
    """
    SQLAlchemy ORM (Object Relational Mapper) 模型，定义了`messages`表的结构。

    这个模型在Python代码和数据库的`messages`表之间建立了一座桥梁。
    该表的核心作用是存储每一次用户与模型之间的完整问答交互记录。
    每一行都代表一次成功的“提问-回答”循环。
    """

    # `__tablename__`是SQLAlchemy的一个特殊属性，它明确指定了这个Python类
    # 对应到数据库中的表名。这里的表名是 'messages'。
    __tablename__ = "messages"

    # `message_id`字段: 表的主键，用于唯一标识每一条消息记录。
    message_id = Column(
        UUID(as_uuid=True),      # 数据类型：PostgreSQL特有的UUID类型。它比自增整数更适合在分布式系统中使用，
                                 #            因为它可以在不同机器上独立生成而几乎不会重复。
                                 # as_uuid=True: 这个参数告诉SQLAlchemy，从数据库读取UUID值时，
                                 #              应将其转换为Python内置的`uuid.UUID`对象，而不是字符串。
        primary_key=True,        # 约束：将此列设置为主键。
        server_default=func.gen_random_uuid() # 默认值：这是一个在数据库服务器层面设置的默认值。
                                              # 当插入新记录时，数据库会自动调用其内置的UUID生成函数
                                              # (如 pgcrypto 的 gen_random_uuid()) 来填充此字段。
    )

    # `session_id`字段: 标识这条消息属于哪个聊天会话。
    session_id = Column(
        String(16),              # 数据类型：可变长度的字符串，最大长度为16个字符。
        nullable=False           # 约束：此列不能为空，确保每条消息都必须归属于一个会话。
    )

    # `user_question`字段: 存储用户提出的原始问题。
    user_question = Column(
        Text,                    # 数据类型：文本类型。与String不同，Text类型通常用于存储非常长的文本数据，
                                 #          因为它在数据库层面没有预设的长度限制。
        nullable=False           # 约束：问题内容不能为空。
    )

    # `model_answer`字段: 存储大语言模型生成的完整回答。
    model_answer = Column(
        Text,                    # 数据类型：文本类型，用于存储可能很长的模型回答。
        nullable=False           # 约束：回答内容不能为空。
    )

    # `create_time`字段: 记录该条消息记录是何时被创建的。
    create_time = Column(
        TIMESTAMP,               # 数据类型：时间戳。
        server_default=func.now()# 默认值：使用数据库服务器的当前时间作为默认值。
                                 #          与'CURRENT_TIMESTAMP'字符串相比，`func.now()`是SQLAlchemy
                                 #          推荐的、跨数据库更通用的写法。
    )

    # `retrieval_content`字段: 存储在RAG流程中，从知识库检索出的、用于生成回答的上下文内容。
    retrieval_content = Column(
        Text,                    # 数据类型：文本类型，因为检索出的内容可能很长。
                                 # 注意：此列没有 `nullable=False` 约束，意味着它可以为空。
                                 # 这是合理的，因为有些对话可能不涉及知识库检索，只是普通的聊天。
                                 # 这个字段对于调试、分析模型回答的依据以及在前端展示引用来源非常重要。
    )

    # 预计算摘要及向量特征（用于 STM 选择与压缩）
    user_summary = Column(Text, nullable=True)
    assistant_summary = Column(Text, nullable=True)
    user_embedding = Column(JSON, nullable=True)
    assistant_embedding = Column(JSON, nullable=True)

# 注释掉重复的 KnowledgeBase 类定义，已迁移到 models/knowledgebase.py
# class KnowledgeBase(Base):
#     __tablename__ = 'knowledgebases'
#     
#     id = Column(Integer, primary_key=True, autoincrement=True)
#     user_id = Column(String(255), nullable=False)
#     file_name = Column(String(255), nullable=False)
#     created_at = Column(TIMESTAMP, nullable=False, server_default='CURRENT_TIMESTAMP')
#     updated_at = Column(TIMESTAMP, nullable=False, server_default='CURRENT_TIMESTAMP')

