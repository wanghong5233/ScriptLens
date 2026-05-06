from sqlalchemy import Column, Integer, String, TIMESTAMP
from sqlalchemy.sql import func
from models.base import Base

class DocumentUpload(Base):
    """
    SQLAlchemy ORM 模型，用于映射数据库中的 `document_uploads` 表。

    该表记录了每一次通过HTTP接口上传的文档的元数据信息。
    它作为一个日志，详细描述了每个上传事件的上下文和基本属性。
    """
    
    # __tablename__ 是一个特殊的类属性，用于告诉SQLAlchemy
    # 这个模型类应该映射到数据库中的哪张表。
    __tablename__ = 'document_uploads'
    
    # 定义表中的列 (Column)。每个类属性都代表表中的一个字段。
    
    # id: 主键
    # - Integer: 数据类型为整数。
    # - primary_key=True: 将此列设置为主键，确保其值的唯一性。
    # - autoincrement=True: 表示该列的值由数据库自动生成和递增，
    #   通常用于创建自增ID。
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # session_id: 上传操作所属的会话ID。
    # - String(16): 数据类型为字符串，最大长度为16个字符。
    # - nullable=False: 表示此列不允许为空 (NOT NULL)。
    #   用于将上传的文档与一个特定的聊天会话关联起来。
    session_id = Column(String(16), nullable=False)
    
    # document_name: 上传的原始文件名。
    # - String(255): 数据类型为字符串，最大长度为255个字符。
    # - nullable=False: 文件名不能为空。
    document_name = Column(String(255), nullable=False)
    
    # document_type: 文档的类型（通常是文件扩展名）。
    # - String(50): 数据类型为字符串，最大长度为50个字符。
    # - nullable=False: 文档类型不能为空。例如："pdf", "docx", "txt"。
    document_type = Column(String(50), nullable=False)
    
    # file_size: 文件的大小（以字节为单位）。
    # - Integer: 数据类型为整数。此列可以为空。
    file_size = Column(Integer)
    
    # upload_time: 文件上传的确切时间戳。
    # - TIMESTAMP: 数据类型为时间戳。
    # - nullable=False: 上传时间不能为空。
    # - server_default=func.now(): 这是一个服务器端的默认值。
    #   当插入一条新记录而没有提供此字段的值时，数据库会自动
    #   使用其当前的系统时间 (NOW()) 来填充它。
    upload_time = Column(TIMESTAMP, nullable=False, server_default=func.now())
    
    # created_at: 记录创建的时间戳。
    #   功能与 upload_time 类似，通常用于标准的记录审计。
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())
    
    # updated_at: 记录最后一次更新的时间戳。
    #   虽然在这个特定的日志型表中，记录可能不常被更新，但
    #   这是一个良好的数据库设计实践。
    #   注意：更完整的实现可能会使用 onupdate=func.now()，
    #   以便在记录更新时自动更新此时间戳。
    updated_at = Column(TIMESTAMP, nullable=False, server_default=func.now()) 