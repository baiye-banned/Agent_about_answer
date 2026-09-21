import uuid

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database.session import Base


def _new_id() -> str:
    return str(uuid.uuid4())[:8]


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    avatar = Column(String(500), default="")
    created_at = Column(DateTime, server_default=func.now())

    conversations = relationship("Conversation", back_populates="user")


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_knowledge_bases_user_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 归属用户；历史数据为 NULL（不可见），由 scripts/backfill_knowledge_owner.py 回填。
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    conversations = relationship("Conversation", back_populates="knowledge_base")
    files = relationship("KnowledgeFile", back_populates="knowledge_base")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(String(36), primary_key=True, default=_new_id)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=True)
    title = Column(String(200), nullable=False)
    memory_summary = Column(Text, default="")
    memory_summary_upto_message_id = Column(Integer, default=0)
    memory_updated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="conversations")
    knowledge_base = relationship("KnowledgeBase", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation",
                            order_by="Message.created_at",
                            cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(36), ForeignKey("conversations.id"), nullable=False)
    role = Column(String(10), nullable=False)
    content = Column(LONGTEXT, nullable=False)
    sources = Column(Text, default="")
    attachments = Column(Text, default="")
    ragas_status = Column(String(20), default="")
    ragas_scores = Column(Text, default="")
    ragas_error = Column(Text, default="")
    retrieval_trace = Column(LONGTEXT, default="")
    created_at = Column(DateTime, server_default=func.now())

    conversation = relationship("Conversation", back_populates="messages")


class KnowledgeFile(Base):
    __tablename__ = "knowledge_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 归属用户；历史数据为 NULL（不可见），由 scripts/backfill_knowledge_owner.py 回填。
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    # 列表与删除链路都按该列过滤，计数聚合（count_knowledge_files_by_base）也按它
    # GROUP BY：没有索引时这些查询对整个 knowledge_files 表扫一遍（issue #83 第 5 项）。
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    size = Column(Integer, nullable=False)
    content = Column(LONGTEXT, default="")
    created_at = Column(DateTime, server_default=func.now())

    knowledge_base = relationship("KnowledgeBase", back_populates="files")


class ChatTraceSession(Base):
    __tablename__ = "chat_trace_sessions"

    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    conversation_id = Column(String(36), nullable=True)
    message_id = Column(Integer, nullable=True)
    status = Column(String(20), default="running")
    events = Column(LONGTEXT, default="[]")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
