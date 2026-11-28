import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis

from app import main
from app.agent import service as service_module
from app.api.deps import get_request_limits
from app.auth.security import create_access_token
from app.config import settings
from app.database.models import LLMCall
from app.database.session import SessionLocal
from app.main import app
from app.observability.recorder import record
from app.observability.service import get_today_tokens
from app.request_limits import RequestLimits
from app.utils.background import drain
from app.utils.time import utcnow
from tests.test_agent import chunk
from tests.test_integration import make_user

CHAT = "/api/v1/agent/chat"
LOGIN = "/api/v1/auth/login"
REDIS_URL = os.getenv("E1_TEST_REDIS_URL")


@pytest.fixture(params=["local", "redis"] if REDIS_URL else ["local"])
async def limits(request, monkeypatch):
    client = Redis.from_url(REDIS_URL) if request.param == "redis" else None
    store = RequestLimits(client)
    monkeypatch.setitem(app.dependency_overrides, get_request_limits, lambda: store)
    try:
        yield store
    finally:
        await drain()
        await store.close()


def api(ip="203.0.113.1"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 12345)), base_url="http://test",
    )


def auth(user_id):
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


async def test_one_turn_per_user_across_sessions_and_release(db, limits, monkeypatch):
    entered, unblock = asyncio.Event(), asyncio.Event()

    class Model:
        async def stream(self, messages, **kwargs):
            entered.set()
            await unblock.wait()
            yield chunk("完成")

    monkeypatch.setattr(service_module, "get_llm", lambda tier: Model())
    monkeypatch.setattr(settings.memory, "auto_extract", False)
    uid = await make_user()
    async with api() as client:
        first = asyncio.create_task(client.post(CHAT, json={"message": "第一轮"}, headers=auth(uid)))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            second = await asyncio.wait_for(
                client.post(CHAT, json={"message": "第二轮"}, headers=auth(uid)), 2,
            )
            assert second.status_code == 429
            assert second.json()["detail"] == "请等待当前对话完成"
            sessions = await client.get("/api/v1/agent/sessions", headers=auth(uid))
            assert len(sessions.json()) == 1
            active = next(rid for rid, turn in service_module.agent_service._turns.items() if turn[0] == uid)
            cancelled = await client.post(f"/api/v1/agent/turns/{active}/cancel", headers=auth(uid))
            assert cancelled.json()["cancelled"] is True
        finally:
            unblock.set()
            response = await asyncio.wait_for(first, 5)
        assert '"status": "cancelled"' in response.text
        completed = await client.post(CHAT, json={"message": "第三轮"}, headers=auth(uid))
        assert '"status": "completed"' in completed.text
        lease = await limits.acquire_turn(uid, settings.agent.total_timeout)
        assert lease is not None
        await lease.release()


async def test_fixed_window_limits_chat_per_user(db, limits, monkeypatch):
    monkeypatch.setattr(settings.limits, "chat_requests", 1)
    monkeypatch.setattr(settings.limits, "chat_window_seconds", 600)
    uid, other = await make_user(), await make_user()
    invalid = {"message": "测试", "session_id": "missing"}
    async with api() as client:
        assert (await client.post(CHAT, json=invalid, headers=auth(uid))).status_code == 404
        limited = await client.post(CHAT, json=invalid, headers=auth(uid))
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) > 0
        assert (await client.post(CHAT, json=invalid, headers=auth(other))).status_code == 404


async def test_daily_budget_uses_utc_and_blocks_before_creating_session(db, limits, monkeypatch):
    uid, other = await make_user(), await make_user()
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(settings.limits, "daily_llm_tokens", 10)
    async with SessionLocal() as db_session:
        for user, created, prompt, completion in [
            (uid, today - timedelta(seconds=1), 900, 900),
            (uid, today, 7, None),
            (uid, today, None, 3),
            (other, today, 20, 30),
            (uid, today + timedelta(days=1), 900, 900),
        ]:
            db_session.add(LLMCall(
                request_id=uuid4().hex, user_id=user, created_at=created, module="agent",
                model="test", latency_ms=0, status="success",
                prompt_tokens=prompt, completion_tokens=completion,
            ))
        await db_session.commit()

    assert await get_today_tokens(uid) == 10
    async with api() as client:
        response = await client.post(CHAT, json={"message": "测试"}, headers=auth(uid))
        assert response.status_code == 429
        assert f"{today + timedelta(days=1):%Y-%m-%d %H:%M} UTC" in response.json()["detail"]
        assert (await client.get("/api/v1/agent/sessions", headers=auth(uid))).json() == []
        lease = await limits.acquire_turn(uid, settings.agent.total_timeout)
        assert lease is not None
        await lease.release()


async def test_completed_turn_usage_is_visible_to_next_chat(db, limits, monkeypatch):
    class Model:
        async def stream(self, messages, **kwargs):
            record(LLMCall, module="agent", model="test", prompt_tokens=7, completion_tokens=3,
                   latency_ms=0, status="success")
            yield chunk("完成")

    monkeypatch.setattr(service_module, "get_llm", lambda tier: Model())
    monkeypatch.setattr(settings.memory, "auto_extract", False)
    monkeypatch.setattr(settings.limits, "daily_llm_tokens", 10)
    uid = await make_user()
    async with api() as client:
        first = await client.post(CHAT, json={"message": "第一轮"}, headers=auth(uid))
        assert '"status": "completed"' in first.text
        second = await client.post(CHAT, json={"message": "第二轮"}, headers=auth(uid))
        assert second.status_code == 429
        assert "Token 预算" in second.json()["detail"]


async def test_login_lockout_and_success_resets_failures(db, limits):
    username = uuid4().hex
    valid = {"username": username, "password": "valid-password"}
    invalid = {**valid, "password": "wrong-password"}
    async with api() as client, api("203.0.113.2") as other:
        assert (await client.post("/api/v1/auth/register", json=valid)).status_code == 201
        for _ in range(2):
            assert (await client.post(LOGIN, json=invalid)).status_code == 401
        assert (await client.post(LOGIN, json=valid)).status_code == 200
        for _ in range(4):
            assert (await client.post(LOGIN, json=invalid)).status_code == 401
        locked = await client.post(LOGIN, json=invalid)
        assert locked.status_code == 423
        assert 890 <= int(locked.headers["Retry-After"]) <= 900
        spoofed = await client.post(LOGIN, json=valid, headers={"X-Forwarded-For": "198.51.100.1"})
        assert spoofed.status_code == 423
        assert (await other.post(LOGIN, json=valid)).status_code == 200


@pytest.mark.skipif(not REDIS_URL, reason="set E1_TEST_REDIS_URL for cross-client Redis tests")
async def test_lifespan_configures_redis_client(db, monkeypatch):
    async def noop():
        pass

    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setattr(main, "init_db", noop)
    monkeypatch.setattr(main, "init_retriever", noop)
    monkeypatch.setattr(main, "init_article_retriever", lambda: None)
    monkeypatch.setattr(main, "init_meal_retriever", lambda: None)
    async with main.lifespan(app):
        assert await app.state.request_limits.redis.ping()


@pytest.mark.skipif(not REDIS_URL, reason="set E1_TEST_REDIS_URL for cross-client Redis tests")
async def test_atomic_redis_counters_and_expired_owner_cannot_release_new_lease():
    first = RequestLimits(Redis.from_url(REDIS_URL))
    second = RequestLimits(Redis.from_url(REDIS_URL))
    uid = uuid4().hex
    try:
        old = await first.acquire_turn(uid, 1)
        assert await second.acquire_turn(uid, 1) is None
        assert await first.redis.ttl(old.key) > 0
        await first.redis.pexpire(old.key, 0)
        new = await second.acquire_turn(uid, 1)
        assert new is not None
        await old.release()
        assert await first.acquire_turn(uid, 1) is None
        await new.release()
        attempts = await asyncio.gather(*(
            store.chat_retry_after(uid, 3, 600) for store in [first, second] * 5
        ))
        assert attempts.count(0) == 3
    finally:
        await first.close()
        await second.close()
