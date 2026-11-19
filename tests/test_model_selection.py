"""通过真实聊天接口验证每轮模型选择，不调用远程 LLM。"""
from types import SimpleNamespace

import httpx

from app.agent import service as service_module
from app.auth.security import create_access_token
from app.config import settings
from app.conversation import repository as repo
from app.main import app
from app.utils.background import drain
from tests.test_integration import make_user


async def test_chat_routes_each_turn_to_selected_model_and_rejects_invalid_tiers(db, monkeypatch):
    selected = []

    class Model:
        def __init__(self, tier):
            self.tier = tier

        async def stream(self, messages, **kwargs):
            yield SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(
                content=f"回答来自 {self.tier}", tool_calls=None,
            ))])

    def get_model(tier):
        selected.append(tier)
        return Model(tier)

    monkeypatch.setattr(service_module, "get_llm", get_model)
    monkeypatch.setattr(settings.memory, "auto_extract", False)
    monkeypatch.setattr(settings.evaluation, "enabled", False)
    uid = await make_user()
    headers = {"Authorization": f"Bearer {create_access_token(uid)}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for tier in ["fast", "normal", None]:
            body = {"message": "你好"}
            sessions = await repo.list_sessions(uid)
            if sessions:
                body["session_id"] = sessions[0].id
            if tier:
                body["model_tier"] = tier
            response = await client.post('/api/v1/agent/chat', headers=headers, json=body)
            assert response.status_code == 200
            assert f"回答来自 {tier or 'normal'}" in response.text
            assert response.text.count("event: done") == 1
            await drain()
        assert selected == ["fast", "normal", "normal"]
        sessions = await repo.list_sessions(uid)
        assert len(sessions) == 1  # 同一会话可以在不同轮次切换
        before = len(await repo.load_messages(sessions[0].id))
        for tier in ["arbitrary-model", "", None, 1]:
            response = await client.post('/api/v1/agent/chat', headers=headers,
                                         json={"message": "你好", "session_id": sessions[0].id, "model_tier": tier})
            assert response.status_code == 422
        assert len(selected) == 3
        assert len(await repo.load_messages(sessions[0].id)) == before
