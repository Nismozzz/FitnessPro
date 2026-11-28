"""会话与消息存储。所有对外查询都必须带 user_id 做归属校验。"""

from __future__ import annotations

from sqlalchemy import delete, func, select, update

from app.database.models import AgentSession, AgentTurn, Message
from app.database.session import SessionLocal
from app.llm.tokens import message_tokens
from app.utils.time import utcnow

RETRYABLE_TURN_STATUSES = ("interrupted", "error", "timeout", "max_iterations")


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


class TurnConflict(Exception):
    pass


async def _lock_session(db, session_id: str, user_id: str | None = None) -> bool:
    stmt = update(AgentSession).where(AgentSession.id == session_id)
    if user_id is not None:
        stmt = stmt.where(AgentSession.user_id == user_id)
    result = await db.execute(stmt.values(updated_at=AgentSession.updated_at))
    return result.rowcount == 1


async def delete_session(user_id: str, session_id: str) -> bool:
    async with SessionLocal() as db:
        if not await _lock_session(db, session_id, user_id):
            return False
        running = await db.scalar(select(AgentTurn.id).where(
            AgentTurn.session_id == session_id, AgentTurn.status == "running"
        ).limit(1))
        if running:
            raise TurnConflict("请等待当前对话完成后再删除会话")
        await db.execute(delete(Message).where(Message.session_id == session_id))
        await db.execute(delete(AgentSession).where(AgentSession.id == session_id))
        await db.commit()
        return True


async def append_messages(session_id: str, turn_id: str, messages: list[dict]) -> None:
    """按顺序追加消息。seq 在会话内单调递增；同一会话的写入由上层会话锁串行化。"""
    if not messages:
        return
    async with SessionLocal() as db:
        await _lock_session(db, session_id)
        await _append_messages(db, session_id, turn_id, messages)
        await db.commit()


async def _append_messages(db, session_id: str, turn_id: str, messages: list[dict]) -> None:
    max_seq = await db.scalar(select(func.max(Message.seq)).where(Message.session_id == session_id))
    now = utcnow()
    for seq, message in enumerate(messages, start=(max_seq or 0) + 1):
        db.add(Message(
            session_id=session_id, seq=seq, turn_id=turn_id, role=message["role"],
            content=message.get("content"), tool_calls=message.get("tool_calls"),
            tool_call_id=message.get("tool_call_id"), name=message.get("name"),
            token_count=message_tokens(message), created_at=now,
        ))
    await db.execute(update(AgentSession).where(AgentSession.id == session_id).values(updated_at=now))


async def create_turn(user_id: str, session_id: str | None, title: str, request_id: str,
                      request: dict, message: str, lease_owner: str) -> AgentTurn | None:
    async with SessionLocal() as db:
        if session_id is None:
            session = AgentSession(user_id=user_id, title=title)
            db.add(session)
            await db.flush()
            session_id = session.id
        elif not await _lock_session(db, session_id, user_id):
            return None
        turn = AgentTurn(id=request_id, session_id=session_id, request_id=request_id,
                         request=request, status="running", lease_owner=lease_owner)
        db.add(turn)
        await _append_messages(db, session_id, turn.id, [{"role": "user", "content": message}])
        await db.commit()
        return turn


async def get_owned_turn(user_id: str, turn_id: str) -> AgentTurn | None:
    async with SessionLocal() as db:
        return await db.scalar(select(AgentTurn).join(AgentSession).where(
            AgentTurn.id == turn_id, AgentSession.user_id == user_id
        ))


async def latest_turn(session_id: str) -> AgentTurn | None:
    async with SessionLocal() as db:
        return await db.scalar(select(AgentTurn).where(AgentTurn.session_id == session_id)
                               .order_by(AgentTurn.created_at.desc(), AgentTurn.id.desc()).limit(1))


async def retry_turn(user_id: str, turn: AgentTurn, request_id: str, lease_owner: str) -> AgentTurn:
    async with SessionLocal() as db:
        if not await _lock_session(db, turn.session_id, user_id):
            raise TurnConflict("会话已删除")
        latest = await db.scalar(select(Message.turn_id).where(Message.session_id == turn.session_id)
                                 .order_by(Message.seq.desc()).limit(1))
        if latest != turn.id:
            raise TurnConflict("只能重试会话中最新一轮未完成的对话")
        current = await db.scalar(update(AgentTurn).where(
            AgentTurn.id == turn.id, AgentTurn.request_id == turn.request_id,
            AgentTurn.status.in_(RETRYABLE_TURN_STATUSES),
        ).values(request_id=request_id, status="running", lease_owner=lease_owner,
                 updated_at=utcnow()).returning(AgentTurn))
        if current is None:
            raise TurnConflict("本轮状态已变化，请刷新后查看")
        await db.execute(delete(Message).where(
            Message.session_id == turn.session_id, Message.turn_id == turn.id,
            Message.role != "user",
        ))
        await db.commit()
        return current


async def save_turn_checkpoint(turn: AgentTurn, checkpoint: dict) -> None:
    async with SessionLocal() as db:
        result = await db.execute(update(AgentTurn).where(
            AgentTurn.id == turn.id, AgentTurn.request_id == turn.request_id,
            AgentTurn.status == "running",
        ).values(checkpoint=checkpoint, updated_at=utcnow()))
        if result.rowcount != 1:
            raise TurnConflict("本轮任务状态已变化")
        await db.commit()


async def finish_turn(turn: AgentTurn, status: str, transcript: list[dict]) -> str:
    async with SessionLocal() as db:
        await _lock_session(db, turn.session_id)
        current = await db.get(AgentTurn, turn.id)
        if current is None or current.request_id != turn.request_id:
            return "interrupted"
        if current.status == "running":
            if transcript:
                await _append_messages(db, turn.session_id, turn.id, transcript)
            current.status = status
            if status in ("completed", "cancelled"):
                current.checkpoint = None
            current.updated_at = utcnow()
            await db.commit()
        return current.status


async def clear_turn_lease(turn_id: str, owner: str) -> None:
    async with SessionLocal() as db:
        await db.execute(update(AgentTurn).where(AgentTurn.id == turn_id, AgentTurn.lease_owner == owner)
                         .values(lease_owner=None))
        await db.commit()


async def interrupt_running_turns() -> list[tuple[str, str, str]]:
    """用于后端单进程启动，返回可能还留在 Redis 的旧锁。"""
    async with SessionLocal() as db:
        await db.execute(update(AgentTurn).where(AgentTurn.status == "running")
                         .values(status="interrupted", updated_at=utcnow()))
        leases = await db.execute(
            select(AgentTurn.id, AgentSession.user_id, AgentTurn.lease_owner)
            .join(AgentSession).where(AgentTurn.lease_owner.is_not(None))
        )
        await db.commit()
        return [(turn_id, user_id, owner) for turn_id, user_id, owner in leases if owner is not None]


async def load_turn_messages(session_id: str, turn_id: str) -> list[Message]:
    async with SessionLocal() as db:
        result = await db.scalars(select(Message).where(Message.session_id == session_id,
                                                       Message.turn_id == turn_id).order_by(Message.seq))
        return list(result)


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
