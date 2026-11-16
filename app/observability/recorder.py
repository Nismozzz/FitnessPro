import logging
from datetime import timedelta

from app.utils.time import utcnow

from app.observability.context import trace_context
from app.utils.background import spawn

logger = logging.getLogger(__name__)


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
        spawn(task_coro, name=f"telemetry:{record_type.__tablename__}")
    except Exception:
        if task_coro is not None:
            task_coro.close()
        logger.warning("Telemetry scheduling failed", exc_info=True)
