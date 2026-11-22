import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta

from app.utils.time import utcnow

from app.observability.context import trace_context
from app.utils.background import spawn

logger = logging.getLogger(__name__)
_turn_usage: ContextVar[set[asyncio.Task] | None] = ContextVar("turn_usage", default=None)


@asynccontextmanager
async def flush_turn_usage():
    """在对话锁释放前提交本轮的用量记录。"""
    writes: set[asyncio.Task] = set()
    token = _turn_usage.set(writes)
    try:
        yield
    finally:
        _turn_usage.reset(token)
        if writes:
            await asyncio.gather(*writes, return_exceptions=True)


async def _persist(record_type, values):
    try:
        from app.database.session import SessionLocal
        async with SessionLocal() as db:
            db.add(record_type(**values))
            await db.commit()
    except Exception:
        logger.warning("Telemetry write failed", exc_info=True)


def record(record_type, **values):
    """Snapshot context before creating the task; never store prompts or credentials."""
    ctx = trace_context.get()
    if ctx is None:
        return
    task_coro = None
    try:
        task_coro = _persist(record_type, {
            "request_id": ctx.request_id, "user_id": ctx.user_id,
            "session_id": ctx.session_id,
            "created_at": utcnow() - timedelta(milliseconds=values.get("latency_ms", 0)),
            **values,
        })
        write = spawn(task_coro, name=f"telemetry:{record_type.__tablename__}")
        writes = _turn_usage.get()
        if writes is not None and record_type.__tablename__ == "llm_calls":
            writes.add(write)
    except Exception:
        if task_coro is not None:
            task_coro.close()
        logger.warning("Telemetry scheduling failed", exc_info=True)
