from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from uuid import uuid4


@dataclass(frozen=True)
class TraceContext:
    request_id: str
    user_id: str | None = None
    session_id: str | None = None
    module: str = "agent"


trace_context: ContextVar[TraceContext | None] = ContextVar("trace_context", default=None)


@contextmanager
def trace_scope(**values):
    current = trace_context.get() or TraceContext(request_id=uuid4().hex)
    token = trace_context.set(replace(current, **values))
    try:
        yield trace_context.get()
    finally:
        trace_context.reset(token)


def traced_module(module):
    def decorate(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            with trace_scope(module=module):
                return await fn(*args, **kwargs)
        return wrapped
    return decorate
