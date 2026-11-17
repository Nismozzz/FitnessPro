"""工具选择同时约束模型 schema 与执行器，不访问真实 LLM。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.agent import service as service_module
from app.agent.tools.base import ToolContext, ToolExecutor
from app.agent.types import ToolCall
from app.auth.security import create_access_token
from app.config import settings
from app.conversation import repository as repo
from app.main import app
from app.utils.background import drain
from tests.test_integration import make_user


async def test_catalog_requires_auth_and_invalid_selection_does_not_create_session(db):
    uid = await make_user()
    headers = {"Authorization": f"Bearer {create_access_token(uid)}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get('/api/v1/agent/tools')).status_code == 401
        response = await client.get('/api/v1/agent/tools', headers=headers)
        assert response.status_code == 200
        assert 'get_current_time' in [t['name'] for t in response.json()]
        for value in [['unknown'], 'get_current_time', [123]]:
            response = await client.post('/api/v1/agent/chat', headers=headers,
                                         json={'message': '你好', 'selected_tools': value})
            assert response.status_code == 422
        assert not await repo.list_sessions(uid)


async def test_each_turn_filters_schemas_including_empty_selection(db, monkeypatch):
    seen = []

    class Model:
        async def stream(self, messages, **kwargs):
            seen.append([s['function']['name'] for s in kwargs.get('tools') or []])
            yield SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content='你好', tool_calls=None))])

    monkeypatch.setattr(service_module, 'get_llm', lambda tier: Model())
    monkeypatch.setattr(settings.memory, 'auto_extract', False)
    monkeypatch.setattr(settings.evaluation, 'enabled', False)
    uid = await make_user()
    headers = {'Authorization': f'Bearer {create_access_token(uid)}'}
    session_id = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for selection in [None, ['get_current_time'], [], ['get_current_time', 'get_current_time']]:
            body = {'message': '你好', 'session_id': session_id}
            if selection is not None:
                body['selected_tools'] = selection
            response = await client.post('/api/v1/agent/chat', headers=headers, json=body)
            assert response.status_code == 200
            assert response.text.count('event: done') == 1
            await drain()
            session_id = (await repo.list_sessions(uid))[0].id
    assert len(seen[0]) > 1
    assert seen[1:] == [['get_current_time'], [], ['get_current_time']]


async def test_unselected_tool_cannot_execute_even_if_model_requests_it(monkeypatch):
    service = service_module.agent_service
    tool = next(t for t in service._tools if t.name == 'forget_memory')
    run = AsyncMock()
    monkeypatch.setattr(tool, 'run', run)
    for selection in [[], ['get_current_time']]:
        executor = ToolExecutor(service.select_tools(selection))
        result = await executor.execute(ToolCall(id='x', name='forget_memory', arguments={'memory_id': 'secret'}), ToolContext(user_id='u', session_id='s'))
        assert not result.success
    run.assert_not_awaited()
