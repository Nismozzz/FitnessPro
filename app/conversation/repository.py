"""会话与消息存储。所有对外查询都必须带 user_id 做归属校验。"""

from __future__ import annotations

from sqlalchemy import delete, func, select, update

from app.database.models import AgentSession, Message
from app.database.session import SessionLocal
from app.llm.tokens import message_tokens
from app.utils.time import utcnow


async def create_session(user_id: str, title: str) -> AgentSession:
    async with SessionLocal() as db:
        now = utcnow()
        session = AgentSession(user_id=user_id, title=title[:100] or "新对话", created_at=now, updated_at=now)
        db.add(session)
        await db.commit()
        return session


async def get_owned_session(user_id: str, session_id: str) -> AgentSession | None:
    async with SessionLocal() as db:
        stmt = select(AgentSession).where(
            AgentSession.id == session_id, AgentSession.user_id == user_id
        )
        return (await db.execute(stmt)).scalar_one_or_none()


async def get_session(session_id: str) -> AgentSession | None:
    """内部使用：调用方必须已经完成归属校验。"""
    async with SessionLocal() as db:
        return await db.get(AgentSession, session_id)


async def list_sessions(user_id: str, limit: int = 50) -> list[AgentSession]:
    async with SessionLocal() as db:
        stmt = (
            select(AgentSession)
            .where(AgentSession.user_id == user_id)
            .order_by(AgentSession.updated_at.desc())
            .limit(limit)
        )
        return list((await db.execute(stmt)).scalars().all())


async def delete_session(user_id: str, session_id: str) -> bool:
    async with SessionLocal() as db:
        owned = (
            await db.execute(
                select(AgentSession.id).where(
                    AgentSession.id == session_id, AgentSession.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if owned is None:
            return False
        await db.execute(delete(Message).where(Message.session_id == session_id))
        await db.execute(delete(AgentSession).where(AgentSession.id == session_id))
        await db.commit()
        return True


async def append_messages(session_id: str, turn_id: str, messages: list[dict]) -> None:
    """按顺序追加消息。seq 在会话内单调递增；同一会话的写入由上层会话锁串行化。"""
    if not messages:
        return
    async with SessionLocal() as db:
        max_seq = (
            await db.execute(select(func.max(Message.seq)).where(Message.session_id == session_id))
        ).scalar()
        next_seq = (max_seq or 0) + 1
        now = utcnow()
        for offset, m in enumerate(messages):
            db.add(
                Message(
                    session_id=session_id,
                    seq=next_seq + offset,
                    turn_id=turn_id,
                    role=m["role"],
                    content=m.get("content"),
                    tool_calls=m.get("tool_calls"),
                    tool_call_id=m.get("tool_call_id"),
                    name=m.get("name"),
                    token_count=message_tokens(m),
                    created_at=now,
                )
            )
        await db.execute(
            update(AgentSession).where(AgentSession.id == session_id).values(updated_at=now)
        )
        await db.commit()


async def load_messages(session_id: str, from_seq: int = 0) -> list[Message]:
    async with SessionLocal() as db:
        stmt = (
            select(Message)
            .where(Message.session_id == session_id, Message.seq >= from_seq)
            .order_by(Message.seq)
        )
        return list((await db.execute(stmt)).scalars().all())


async def update_summary(
    session_id: str, expected_version: int, summary: str, upto_seq: int
) -> bool:
    """乐观锁更新摘要：版本不一致说明已被其他压缩任务更新，放弃本次结果。"""
    async with SessionLocal() as db:
        result = await db.execute(
            update(AgentSession)
            .where(AgentSession.id == session_id, AgentSession.version == expected_version)
            .values(
                summary=summary,
                summary_upto_seq=upto_seq,
                version=expected_version + 1,
            )
        )
        await db.commit()
        return result.rowcount == 1
