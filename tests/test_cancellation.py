import asyncio

import httpx
import pytest

from app.agent import service as service_module
from app.agent.service import AgentService
from app.agent.tools.base import BaseTool
from app.api import agent as agent_api
from app.auth.security import create_access_token
from app.config import settings
from app.conversation import repository as repo
from app.main import app
from app.utils.background import drain
from tests.test_agent import chunk, tc
from tests.test_integration import make_user


@pytest.mark.parametrize("target", ["model", "tool"])
async def test_cancel_interrupts_work_and_does_not_continue(db, monkeypatch, target):
    entered, closed = asyncio.Event(), asyncio.Event()
    never = asyncio.Event()

    class BlockingTool(BaseTool):
        name = "blocking"
        description = "test"

        async def run(self, params, ctx):
            entered.set()
            try:
                await never.wait()
            finally:
                closed.set()

    class Model:
        calls = 0

        async def stream(self, messages, **kw):
            self.calls += 1
            if target == "tool":
                yield chunk(tool_calls=[tc(0, "c1", "blocking", "{}")])
            else:
                entered.set()
                try:
                    await never.wait()
                    yield chunk("不应该生成")
                finally:
                    closed.set()

    model = Model()
    monkeypatch.setattr(service_module, "get_llm", lambda tier: model)
    monkeypatch.setattr(settings.memory, "auto_extract", False)
    service = AgentService()
    service._tools = [BlockingTool()]
    monkeypatch.setattr(agent_api, "agent_service", service)
    uid, other = await make_user(), await make_user()
    sid = await service.prepare_session(uid, None, "制定计划")
    queue = service.start_turn(user_id=uid, session_id=sid, message="制定计划")
    first = await queue.get()
    rid = first.data["request_id"]
    await asyncio.wait_for(entered.wait(), 2)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/v1/agent/turns/{rid}/cancel"
        assert (await client.post(path)).status_code == 401
        foreign = await client.post(path, headers={"Authorization": f"Bearer {create_access_token(other)}"})
        assert foreign.json()["cancelled"] is False
        assert not closed.is_set()
        result = await client.post(path, headers={"Authorization": f"Bearer {create_access_token(uid)}"})
        assert result.json()["cancelled"] is True
        assert closed.is_set()
        again = await client.post(path, headers={"Authorization": f"Bearer {create_access_token(uid)}"})
        assert again.json()["cancelled"] is False

    events = []
    while (event := await asyncio.wait_for(queue.get(), 2)) is not None:
        events.append(event)
    assert [e.data["status"] for e in events if e.type == "done"] == ["cancelled"]
    assert model.calls == 1
    rows = await repo.load_messages(sid)
    assert rows[0].role == "user"
    assert not any(r.content == "不应该生成" for r in rows)
    await drain()


async def test_cancel_before_start_and_while_waiting_for_session_lock(db):
    service = AgentService()
    uid = await make_user()
    sid = await service.prepare_session(uid, None, "制定计划")
    async with service._lock(sid):
        for queued in (False, True):
            queue = service.start_turn(user_id=uid, session_id=sid, message="制定计划")
            rid = (await queue.get()).data["request_id"]
            if queued:
                await asyncio.sleep(0)
            assert await service.cancel_turn(uid, rid)
            assert (await queue.get()).data["status"] == "cancelled"
            assert await queue.get() is None
    assert not await repo.load_messages(sid)
