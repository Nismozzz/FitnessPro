"""Share a transaction between an Agent tool's mutation and its saved result."""

from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import SessionLocal


tool_transaction: ContextVar[AsyncSession | None] = ContextVar("tool_transaction", default=None)


@asynccontextmanager
async def mutation_session():
    session = tool_transaction.get()
    if session is not None:
        yield session
        await session.flush()
        return
    async with SessionLocal.begin() as session:
        yield session
