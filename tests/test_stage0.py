"""Stage 0 acceptance: real API + SQLite, deterministic streamed model/extractor."""
from types import SimpleNamespace

import httpx

from app.agent import service as service_module
from app.auth.security import create_access_token
from app.config import settings
from app.conversation import repository as repo
from app.main import app
from app.memory import extractor
from app.utils.background import drain
from tests.test_integration import make_user


async def test_stage0_candidate_edit_confirm_next_turn_and_ownership(db, monkeypatch):
    class Coach:
        def __init__(self):
            self.seen = []

        async def stream(self, messages, **kw):
            self.seen.append([dict(m) for m in messages])
            for reasoning, content in [("检查身体限制", None), (None, "注意保护左膝，先了解伤病情况。")]:
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                    content=content, reasoning_content=reasoning, tool_calls=None
                ))], usage=None)

    class Extractor:
        async def complete_json(self, messages):
            return {"operations": [{"kind": "injury", "content": "左膝有旧伤", "confidence": 0.95}]}

    coach = Coach()
    monkeypatch.setattr(service_module, "get_llm", lambda tier="normal": coach)
    monkeypatch.setattr(extractor, "get_llm", lambda tier="fast": Extractor())
    monkeypatch.setattr(settings.memory, "auto_extract", True)
    uid, other = await make_user(), await make_user()
    headers = {"Authorization": f"Bearer {create_access_token(uid)}"}
    other_headers = {"Authorization": f"Bearer {create_access_token(other)}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post('/api/v1/agent/chat', headers=headers, json={"message": "我左膝有旧伤"})
        assert response.status_code == 200
        assert 'event: thinking' not in response.text
        assert '检查身体限制' not in response.text
        assert response.text.count('event: done') == 1
        await drain()
        candidates = (await client.get('/api/v1/memories?status=candidate', headers=headers)).json()
        assert len(candidates) == 1 and candidates[0]['content'] == '左膝有旧伤'
        mid = candidates[0]['id']
        assert (await client.get('/api/v1/memories?status=candidate', headers=other_headers)).json() == []
        for method, path, body in [('POST', f'/{mid}/confirm', None), ('POST', f'/{mid}/reject', None), ('PATCH', f'/{mid}', {'content': '其他用户修改'}), ('DELETE', f'/{mid}', None)]:
            result = await client.request(method, '/api/v1/memories' + path, headers=other_headers, json=body)
            assert result.status_code == 404
        edited = await client.patch(f'/api/v1/memories/{mid}', headers=headers, json={'content': '左膝有旧伤，避免跳跃'})
        assert edited.json()['status'] == 'candidate'
        confirmed = await client.post(f'/api/v1/memories/{mid}/confirm', headers=headers)
        assert confirmed.json()['status'] == 'active'
        assert (await client.get('/api/v1/memories?status=candidate', headers=headers)).json() == []
        sid = (await repo.list_sessions(uid))[0].id
        await client.post('/api/v1/agent/chat', headers=headers, json={'message': '今天怎么练？', 'session_id': sid})
        await drain()
        system = coach.seen[-1][0]['content']
        assert '用户已确认' in system and '左膝有旧伤，避免跳跃' in system
        assert '检查身体限制' not in str(coach.seen[-1])
        history = (await client.get(f'/api/v1/agent/sessions/{sid}/messages', headers=headers)).json()
        assert '检查身体限制' not in str(history)
