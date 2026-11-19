from uuid import uuid4

from app.observability.context import trace_scope


class RequestTraceMiddleware:
    """Pure ASGI middleware keeps ContextVars isolated and does not buffer SSE."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        with trace_scope(request_id=uuid4().hex, user_id=None, session_id=None, module="agent") as ctx:
            async def traced_send(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    if not any(k.lower() == b"x-request-id" for k, _ in headers):
                        headers.append((b"x-request-id", ctx.request_id.encode()))
                    message = {**message, "headers": headers}
                await send(message)
            await self.app(scope, receive, traced_send)
