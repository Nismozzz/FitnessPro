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
from datetime import date, datetime

from sqlalchemy import Date, Index, text, JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
    training_location: Mapped[str | None] = mapped_column(String(24))
    excluded_equipment: Mapped[list | None] = mapped_column(JSON)
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


class WorkoutPlan(Base):
    __tablename__ = "workout_plans"
    __table_args__ = (
        UniqueConstraint("user_id", "content_hash", name="uq_plan_content"),
        Index("uq_user_active_plan", "user_id", unique=True,
              sqlite_where=text("status = 'active'"), postgresql_where=text("status = 'active'")),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    goal: Mapped[str] = mapped_column(String(32))
    split_type: Mapped[str] = mapped_column(String(32))
    week_start: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    root_id: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64))
    draft: Mapped[dict] = mapped_column(JSON)
    validation: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PlanDay(Base):
    __tablename__ = "plan_days"
    __table_args__ = (UniqueConstraint("plan_id", "weekday", name="uq_plan_weekday"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(ForeignKey("workout_plans.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)
    focus: Mapped[str] = mapped_column(String(100))
    notes: Mapped[str] = mapped_column(Text, default="")
    warmup_minutes: Mapped[int] = mapped_column(Integer, default=5)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=5)


class PlanExercise(Base):
    __tablename__ = "plan_exercises"
    __table_args__ = (UniqueConstraint("plan_day_id", "position", name="uq_day_position"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    plan_day_id: Mapped[str] = mapped_column(ForeignKey("plan_days.id", ondelete="CASCADE"), index=True)
    exercise_id: Mapped[str] = mapped_column(ForeignKey("exercises.id"))
    position: Mapped[int] = mapped_column(Integer)
    sets: Mapped[int] = mapped_column(Integer)
    reps_min: Mapped[int | None] = mapped_column(Integer)
    reps_max: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    rest_seconds: Mapped[int] = mapped_column(Integer)
    rpe: Mapped[float | None]
    target_weight_kg: Mapped[float | None]
    notes: Mapped[str] = mapped_column(Text, default="")
    snapshot: Mapped[dict] = mapped_column(JSON)


class WorkoutLog(Base):
    __tablename__ = "workout_logs"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_log_idempotency"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    plan_day_id: Mapped[str | None] = mapped_column(ForeignKey("plan_days.id"))
    performed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    duration_min: Mapped[int] = mapped_column(Integer)
    rpe: Mapped[float | None]
    notes: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str] = mapped_column(String(100))
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WorkoutSet(Base):
    __tablename__ = "workout_sets"
    __table_args__ = (UniqueConstraint("log_id", "exercise_id", "set_no", name="uq_log_set"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    log_id: Mapped[str] = mapped_column(ForeignKey("workout_logs.id", ondelete="CASCADE"), index=True)
    exercise_id: Mapped[str] = mapped_column(ForeignKey("exercises.id"))
    set_no: Mapped[int] = mapped_column(Integer)
    reps: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    weight_kg: Mapped[float | None]
    rpe: Mapped[float | None]
    completed: Mapped[bool]
    snapshot: Mapped[dict] = mapped_column(JSON)


class RagEvaluation(Base):
    """RAGAS 在线评估结果。一轮对话（turn）至多一条，异步回填分数。"""

    __tablename__ = "rag_evaluations"
    __table_args__ = (
        UniqueConstraint("session_id", "turn_id", name="uq_rag_eval_turn"),
        Index("ix_rag_eval_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True)
    turn_id: Mapped[str] = mapped_column(String(32))
    query: Mapped[str] = mapped_column(Text)
    contexts: Mapped[list] = mapped_column(JSON)
    response: Mapped[str] = mapped_column(Text)
    faithfulness: Mapped[float | None] = mapped_column(nullable=True)
    answer_relevancy: Mapped[float | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/completed/failed
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime)
