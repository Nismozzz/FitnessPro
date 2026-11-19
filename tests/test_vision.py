"""Exercise image boundaries and chat/RAG integration without paid model requests."""
import asyncio
import base64
import io
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from app.agent import service as agent_module
from app.agent.service import AgentService
from app.agent.tools.vision_tools import IdentifyEquipmentTool, IdentifyEquipmentParams
from app.agent.tools.base import ToolContext
from app.auth.security import create_access_token
from app.config import LLMProfile, settings
from app.conversation import repository as repo
from app.main import app
from app.utils.background import drain
from app.vision import service as vision
from app.vision.images import ImageError, image_path, save_image
from tests.test_agent import chunk, tc
from tests.test_integration import make_user


def photo(size=(90, 60), fmt='JPEG'):
    output = io.BytesIO()
    image = Image.new('RGB', size, 'gray')
    exif = image.getexif()
    exif[270] = 'private camera metadata'
    image.save(output, fmt, exif=exif)
    return output.getvalue()


def observation(status='identified'):
    return dict(status=status, candidates=[dict(name='哑铃', equipment='dumbbell',
                visible_features=['两端配重'], search_query='dumbbell')], explanation='可见哑铃外观',
                needs_confirmation=False, follow_up='请确认是否为哑铃')


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.vision, 'enabled', True)
    monkeypatch.setattr(settings.vision, 'upload_dir', str(tmp_path))
    monkeypatch.setattr(settings.llm, 'vision', LLMProfile(model='vision-test', base_url='https://vision.test/v1', api_key_env='VISION_TEST_KEY'))
    monkeypatch.setenv('VISION_TEST_KEY', 'test')
    monkeypatch.setattr(settings.memory, 'auto_extract', False)
    monkeypatch.setattr(settings.evaluation, 'enabled', False)
    return tmp_path


def auth(uid):
    return {'Authorization': f'Bearer {create_access_token(uid)}'}


async def test_upload_auth_owner_validation_and_deletion(db, configured):
    uid, other = await make_user(), await make_user()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post('/api/v1/images', content=photo(), headers={'Content-Type': 'image/jpeg'})).status_code == 401
        response = await client.post('/api/v1/images', content=photo(), headers={**auth(uid), 'Content-Type': 'image/jpeg'})
        assert response.status_code == 201
        image_id = response.json()['id']
        path = f'/api/v1/images/{image_id}'
        response = await client.get(path, headers=auth(uid))
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert not Image.open(io.BytesIO(response.content)).getexif()
        for method in ('get', 'delete'):
            assert (await getattr(client, method)(path, headers=auth(other))).status_code == 404
        for invalid in ('garbage', 'a'*32):
            assert (await client.get('/api/v1/images/' + invalid, headers=auth(uid))).status_code == 404
        assert (await client.post('/api/v1/images', headers={**auth(uid), 'Content-Type': 'image/svg+xml'}, content=b'<svg/>')).status_code == 415
        assert (await client.post('/api/v1/images', headers={**auth(uid), 'Content-Type': 'image/jpeg'}, content=b'fake')).status_code == 422
        assert (await client.delete(path, headers=auth(uid))).status_code == 204
        assert (await client.get(path, headers=auth(uid))).status_code == 404


async def test_upload_limits_and_disabled_mode(db, configured, monkeypatch):
    uid = await make_user()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        monkeypatch.setattr(settings.vision, 'max_upload_bytes', 1024)
        assert (await client.post('/api/v1/images', headers={**auth(uid), 'Content-Type': 'image/jpeg'}, content=b'x'*1025)).status_code == 413
        monkeypatch.setattr(settings.vision, 'enabled', False)
        assert not (await client.get('/api/v1/images/config', headers=auth(uid))).json()['available']
        assert (await client.post('/api/v1/images', headers={**auth(uid), 'Content-Type': 'image/jpeg'}, content=photo())).status_code == 503


def test_normalization_orientation_and_pixel_limit(configured, monkeypatch):
    monkeypatch.setattr(settings.vision, 'max_edge', 64)
    image_id = save_image('u', photo((200, 100)))
    with Image.open(image_path('u', image_id)) as image:
        assert image.size == (64, 32)
        assert image.format == 'JPEG'
        assert not image.getexif()
    monkeypatch.setattr(settings.vision, 'max_pixels', 100)
    with pytest.raises(ImageError, match='分辨率'):
        save_image('u', photo())
    # An EXIF-rotated portrait is displayed with its intended orientation.
    monkeypatch.setattr(settings.vision, 'max_pixels', 20000)
    output = io.BytesIO()
    image = Image.new('RGB', (90, 60))
    exif = image.getexif(); exif[274] = 6
    image.save(output, 'JPEG', exif=exif)
    image_id = save_image('u', output.getvalue())
    with Image.open(image_path('u', image_id)) as image:
        assert image.height > image.width


@pytest.mark.parametrize('status', ['identified', 'uncertain', 'not_equipment'])
async def test_vision_receives_only_normalized_image_and_validates_output(configured, monkeypatch, status):
    model = AsyncMock()
    model.complete_json.return_value = observation(status)
    monkeypatch.setattr(vision, 'get_vision_llm', lambda: model)
    image_id = save_image('u', photo())
    result = await vision.identify_equipment('u', image_id)
    payload = model.complete_json.call_args.args[0]
    url = payload[1]['content'][1]['image_url']['url']
    assert url.startswith('data:image/jpeg;base64,')
    assert base64.b64decode(url.split(',')[1]) == image_path('u', image_id).read_bytes()
    assert result['untrusted_observation']['needs_confirmation']
    if status == 'not_equipment':
        assert result['untrusted_observation']['candidates'] == []
    model.complete_json.return_value = observation() | {'candidates': []}
    with pytest.raises(ValueError):
        await vision.identify_equipment('u', image_id)
    with pytest.raises(FileNotFoundError):
        await vision.identify_equipment('other', image_id)


async def test_chat_checks_images_before_creating_session(db, configured):
    uid, other = await make_user(), await make_user()
    image_id = save_image(other, photo())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for ids, selection, status in [([image_id], None, 404), (['bad'], None, 404), ([image_id, image_id], None, 422), ([image_id], [], 422)]:
            body = dict(message='识别器械', image_ids=ids)
            if selection is not None:
                body['selected_tools'] = selection
            response = await client.post('/api/v1/agent/chat', headers=auth(uid), json=body)
            assert response.status_code == status
        assert not await repo.list_sessions(uid)


async def test_image_chat_then_confirm_and_retrieve_and_delete_session(db, configured, monkeypatch):
    uid = await make_user()
    image_id = save_image(uid, photo())
    vision_model = AsyncMock()
    vision_model.complete_json.return_value = observation()
    monkeypatch.setattr(vision, 'get_vision_llm', lambda: vision_model)
    service = AgentService()
    from app.api import agent as agent_api
    monkeypatch.setattr(agent_api, 'agent_service', service)
    search = next(t for t in service._tools if t.name == 'search_exercises')
    search_run = AsyncMock(return_value={'exercises': [], 'warnings': ['测试无匹配']})
    monkeypatch.setattr(search, 'run', search_run)
    calls = []

    class Model:
        async def stream(self, messages, **kwargs):
            calls.append(messages.copy())
            n = len(calls)
            if n == 1:
                yield chunk(tool_calls=[tc(0, 'v1', 'identify_equipment', json.dumps({'image_id': image_id}))])
            elif n == 2:
                yield chunk('这看起来是哑铃，请确认。')
            elif n == 3:
                yield chunk(tool_calls=[tc(0, 's1', 'search_exercises', '{"query":"dumbbell","equipment":["dumbbell"]}')])
            else:
                yield chunk('当前动作库没有匹配结果。')

    monkeypatch.setattr(agent_module, 'get_llm', lambda tier: Model())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/v1/agent/chat', headers=auth(uid), json={'message': '这是什么器械', 'image_ids': [image_id]})
        assert response.status_code == 200
        assert 'identify_equipment' in response.text and '请确认' in response.text
        await drain()
        sid = (await repo.list_sessions(uid))[0].id
        response = await client.post('/api/v1/agent/chat', headers=auth(uid), json={'message': '确认是哑铃，请推荐动作', 'session_id': sid})
        assert 'search_exercises' in response.text
        await drain()
        search_run.assert_awaited_once()
        assert search_run.call_args.args[0].equipment == ['dumbbell']
        response = await client.get(f'/api/v1/agent/sessions/{sid}/messages', headers=auth(uid))
        stored = json.dumps(response.json(), ensure_ascii=False)
        assert f'[图片附件：{image_id}]' in stored
        assert 'base64' not in stored
        assert 'untrusted_observation' in stored
        assert (await client.delete(f'/api/v1/agent/sessions/{sid}', headers=auth(uid))).status_code == 204
        assert not image_path(uid, image_id).exists()


async def test_cancelling_image_turn_interrupts_vision(db, configured, monkeypatch):
    entered, closed = asyncio.Event(), asyncio.Event()

    class Vision:
        async def complete_json(self, messages):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

    monkeypatch.setattr(vision, 'get_vision_llm', lambda: Vision())
    uid = await make_user()
    image_id = save_image(uid, photo())

    class Model:
        async def stream(self, messages, **kwargs):
            yield chunk(tool_calls=[tc(0, 'v1', 'identify_equipment', json.dumps({'image_id': image_id}))])

    monkeypatch.setattr(agent_module, 'get_llm', lambda tier: Model())
    service = AgentService()
    sid = await service.prepare_session(uid, None, '识别')
    queue = service.start_turn(user_id=uid, session_id=sid, message=f'识别\n[图片附件：{image_id}]')
    request_id = (await queue.get()).data['request_id']
    await asyncio.wait_for(entered.wait(), 2)
    assert await service.cancel_turn(uid, request_id)
    assert closed.is_set()
    events = []
    while (event := await queue.get()) is not None:
        events.append(event)
    assert [e.data['status'] for e in events if e.type == 'done'] == ['cancelled']
    await drain()


async def test_image_turn_cannot_write_profile_even_before_identification(db, configured, monkeypatch):
    monkeypatch.setattr(settings.memory, 'auto_extract', True)
    extract = AsyncMock()
    monkeypatch.setattr(agent_module, 'extract_memories', extract)
    uid = await make_user()
    image_id = save_image(uid, photo())
    service = AgentService()
    from app.api import agent as agent_api
    monkeypatch.setattr(agent_api, 'agent_service', service)
    write = next(tool for tool in service._tools if tool.name == 'update_user_profile')
    write_run = AsyncMock()
    monkeypatch.setattr(write, 'run', write_run)

    class Model:
        count = 0
        async def stream(self, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                yield chunk(tool_calls=[tc(0, 'w1', 'update_user_profile', '{"equipment":["dumbbell"]}')])
            else:
                yield chunk('请先确认器械。')

    monkeypatch.setattr(agent_module, 'get_llm', lambda tier: Model())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/v1/agent/chat', headers=auth(uid), json={'message': '识别器械', 'image_ids': [image_id]})
        assert response.status_code == 200
        assert '仅允许读取信息' in response.text
        write_run.assert_not_awaited()
    await drain()
    extract.assert_not_awaited()


async def test_vision_tool_returns_safe_errors(configured, monkeypatch):
    image_id = save_image('u', photo())
    model = AsyncMock()
    model.complete_json.return_value = observation() | {'candidates': [{'name': 'bad'}]}
    monkeypatch.setattr(vision, 'get_vision_llm', lambda: model)
    from app.agent.tools.base import ToolExecutor
    from app.agent.types import ToolCall
    result = await ToolExecutor([IdentifyEquipmentTool()]).execute(
        ToolCall('v1', 'identify_equipment', {'image_id': image_id}), ToolContext(user_id='u', session_id='s'))
    assert not result.success
    assert 'data:image' not in result.error
    assert '图片识别不可用' in result.error
