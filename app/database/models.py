"""
ORM 模型。

记忆分层（对应设计文档第 4 节）：
- 工作记忆：单次 Agent 执行中的 messages（不落库）
- 会话原始记忆：messages 表
- 会话摘要记忆：agent_sessions.summary / summary_upto_seq
- 用户画像：user_profiles（结构化、用户可编辑）
- 长期事实记忆：memories（带来源、状态、有效期，可确认/删除）
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base
from app.utils.time import utcnow


def new_id() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    gender: Mapped[str | None] = mapped_column(String(16))
    age: Mapped[int | None] = mapped_column(Integer)
    height_cm: Mapped[float | None]
    weight_kg: Mapped[float | None]
    goal: Mapped[str | None] = mapped_column(String(32))
    experience_level: Mapped[str | None] = mapped_column(String(16))
    training_days_per_week: Mapped[int | None] = mapped_column(Integer)
    session_minutes: Mapped[int | None] = mapped_column(Integer)
    equipment: Mapped[list | None] = mapped_column(JSON)
    injuries: Mapped[list | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(100), default="新对话")
    # 摘要覆盖 seq < summary_upto_seq 的全部消息；之后的消息以原文进入上下文
    summary: Mapped[str | None] = mapped_column(Text)
    summary_upto_seq: Mapped[int] = mapped_column(Integer, default=0)
    # 乐观锁：摘要更新时比较版本，避免并发压缩互相覆盖
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_message_session_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    # 同一轮（一次用户输入及其触发的全部工具调用与回答）共享 turn_id，压缩与截断都以轮为单位
    turn_id: Mapped[str] = mapped_column(String(32), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str | None] = mapped_column(Text)
    tool_calls: Mapped[list | None] = mapped_column(JSON)
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(64))
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(String(500))
    scope: Mapped[str] = mapped_column(String(16), default="long_term")
    # candidate（自动提取待确认）/ active / rejected / archived
    status: Mapped[str] = mapped_column(String(16), index=True)
    # user_explicit / auto_extracted / manual
    source: Mapped[str] = mapped_column(String(32))
    source_session_id: Mapped[str | None] = mapped_column(String(32))
    supersedes_id: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Exercise(Base):
    """动作知识库原文（父文档）。向量索引只存 id 与向量。"""

    __tablename__ = "exercises"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    name_zh: Mapped[str | None] = mapped_column(String(200))
    aliases_zh: Mapped[list | None] = mapped_column(JSON)
    force: Mapped[str | None] = mapped_column(String(16))
    level: Mapped[str] = mapped_column(String(16))
    mechanic: Mapped[str | None] = mapped_column(String(16))
    equipment: Mapped[str | None] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(32))
    primary_muscles: Mapped[list] = mapped_column(JSON)
    secondary_muscles: Mapped[list] = mapped_column(JSON)
    instructions: Mapped[list] = mapped_column(JSON)
    instructions_zh: Mapped[list | None] = mapped_column(JSON)
    images: Mapped[list | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
