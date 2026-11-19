"""Instrument non-streaming SDK consumers (RAGAS manages its own SDK client)."""
import asyncio
from functools import wraps
import time

from app.database.models import LLMCall
from app.observability.context import trace_scope
from app.observability.recorder import record


def observe_sdk_client(client, module):
    create = client.chat.completions.create

    @wraps(create)
    async def observed(*args, **kwargs):
        start, response, error, status = time.perf_counter(), None, None, "success"
        with trace_scope(module=module):
            try:
                response = await create(*args, **kwargs)
                return response
            except BaseException as exc:
                error = type(exc).__name__
                status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                raise
            finally:
                usage = getattr(response, "usage", None)
                record(LLMCall, module=module, model=kwargs.get("model", "unknown"),
                       prompt_tokens=getattr(usage, "prompt_tokens", None),
                       completion_tokens=getattr(usage, "completion_tokens", None),
                       latency_ms=(time.perf_counter() - start) * 1000,
                       first_token_ms=None, status=status, error=error)

    client.chat.completions.create = observed
    return client
