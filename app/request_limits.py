"""HTTP 入口使用的每用户对话锁、请求计数与登录失败记录。"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

PREFLIGHT_TIMEOUT = 20
TURN_OVERHEAD = 30

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

_INCREMENT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""

_LOGIN = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
if count < limit then
    if ARGV[3] == 'success' then
        redis.call('DEL', KEYS[1])
    elseif ARGV[3] == 'failure' then
        count = redis.call('INCR', KEYS[1])
        if count == 1 or count == limit then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
    end
end
if count >= limit then
    return math.max(1, math.ceil(redis.call('PTTL', KEYS[1]) / 1000))
end
return 0
"""


@dataclass(frozen=True)
class TurnLease:
    limits: RequestLimits
    key: str
    owner: str

    async def release(self) -> None:
        try:
            if self.limits.redis is not None:
                await self.limits.redis.eval(_RELEASE, 1, self.key, self.owner)
            elif self.limits._get(self.key) == self.owner:
                self.limits._delete(self.key)
        except RedisError:
            logger.warning("释放对话锁失败，等待 Redis TTL 到期", exc_info=True)


class RequestLimits:
    def __init__(self, redis: Redis | None = None) -> None:
        self.redis = redis
        self._local: dict[str, tuple[int | str, asyncio.TimerHandle]] = {}

    def _delete(self, key: str) -> None:
        entry = self._local.pop(key, None)
        if entry is not None:
            entry[1].cancel()

    def _get(self, key: str) -> int | str | None:
        entry = self._local.get(key)
        if entry is None:
            return None
        if entry[1].when() <= asyncio.get_running_loop().time():
            self._delete(key)
            return None
        return entry[0]

    def _set(self, key: str, value: int | str, ttl: int) -> None:
        entry = self._local.get(key)
        timer = entry[1] if entry is not None else asyncio.get_running_loop().call_later(ttl, self._delete, key)
        self._local[key] = (value, timer)

    async def acquire_turn(self, user_id: str, turn_timeout: float) -> TurnLease | None:
        key = f"fitnesspro:lock:turn:{user_id}"
        owner = uuid4().hex
        ttl = math.ceil(turn_timeout) + PREFLIGHT_TIMEOUT + TURN_OVERHEAD + 10
        if self.redis is not None:
            acquired = await self.redis.set(key, owner, nx=True, ex=ttl)
            return TurnLease(self, key, owner) if acquired else None
        if self._get(key) is not None:
            return None
        self._set(key, owner, ttl)
        return TurnLease(self, key, owner)

    async def chat_retry_after(self, user_id: str, limit: int, window: int) -> int:
        now = time.time()
        bucket = int(now) // window
        key = f"fitnesspro:rl:chat:{user_id}:{bucket}"
        remaining = math.ceil((bucket + 1) * window - now)
        if self.redis is not None:
            count = await self.redis.eval(_INCREMENT, 1, key, window * 2)
        else:
            count = int(self._get(key) or 0) + 1
            self._set(key, count, window * 2)
        return remaining if count > limit else 0

    async def login_retry_after(
        self, ip: str, username: str, limit: int, window: int,
        action: Literal["check", "success", "failure"] = "check",
    ) -> int:
        key = f"fitnesspro:fail:{ip}:{username}"
        if self.redis is not None:
            return await self.redis.eval(_LOGIN, 1, key, limit, window, action)

        count = int(self._get(key) or 0)
        if count < limit:
            if action == "success":
                self._delete(key)
            elif action == "failure":
                count += 1
                if count == limit:
                    self._delete(key)
                self._set(key, count, window)
        if count >= limit:
            return max(1, math.ceil(self._local[key][1].when() - asyncio.get_running_loop().time()))
        return 0

    async def close(self) -> None:
        for key in list(self._local):
            self._delete(key)
        if self.redis is not None:
            await self.redis.aclose()
