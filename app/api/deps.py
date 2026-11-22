from fastapi import Depends, HTTPException, Request

from app.auth.security import get_current_user_id
from app.config import settings
from app.request_limits import RequestLimits


async def get_request_limits(request: Request) -> RequestLimits:
    return request.app.state.request_limits


async def rate_limit(
    user_id: str = Depends(get_current_user_id),
    limits: RequestLimits = Depends(get_request_limits),
) -> None:
    config = settings.limits
    retry = await limits.chat_retry_after(user_id, config.chat_requests, config.chat_window_seconds)
    if retry:
        raise HTTPException(429, "请求过于频繁，请稍后再试", headers={"Retry-After": str(retry)})
