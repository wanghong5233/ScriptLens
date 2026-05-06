from sqlalchemy import Boolean, Column, Integer, String
from sqlalchemy.orm import relationship
from models.base import Base

class User(Base):
    """
    SQLAlchemy ORM 模型，用于映射数据库中的 `users` 表。

    该表存储了应用中的用户信息，包括登录凭证和唯一标识。
    """
    __tablename__ = 'users'

    # id: 用户的主键ID。
    # - Integer: 数据类型为整数。
    # - primary_key=True: 将此列设置为主键。SQLAlchemy默认会为整数主键开启自动增长。
    id = Column(Integer, primary_key=True)

    # username: 用户的登录名。
    # - String(50): 数据类型为字符串，最大长度为50。
    # - unique=True: 在表中此列的值必须是唯一的，用于防止用户名重复。
    # - nullable=False: 此列不允许为空。
    username = Column(String(50), unique=True, nullable=False)

    # role: 用户角色（user/admin/super_admin）
    role = Column(String(32), nullable=False, default="user", server_default="user")

    # is_active: 用户是否可用（false 表示被封禁/停用）
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")

    # password_hash: 存储用户密码的哈希值。
    # - String(100): 数据类型为字符串，长度设为100以容纳哈希算法生成的字符串。
    # - nullable=False: 密码哈希值不能为空。
    #   重要：出于安全考虑，永远不要直接存储明文密码，只存储其哈希值。
    password_hash = Column(String(100), nullable=False)

    # 建立与KnowledgeBase模型的一对多关系
    # a. back_populates="user": 指定了在KnowledgeBase模型中，可以通过user属性反向访问这个User实例。
    # b. cascade="all, delete-orphan": 级联操作，确保当删除一个User时，其拥有的所有KnowledgeBase也会被一并删除。
    knowledge_bases = relationship("KnowledgeBase", back_populates="user", cascade="all, delete-orphan")