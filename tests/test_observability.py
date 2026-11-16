import asyncio
from types import SimpleNamespace as NS

import httpx
import pytest
from sqlalchemy import select

from app.agent.tools.base import ToolContext, ToolExecutor
from app.agent.types import ToolCall
from app.auth.security import create_access_token
from app.config import ModelPrice, settings
from app.database.models import LLMCall, RetrievalLog, ToolCallLog, new_id
from app.database.session import SessionLocal
from app.llm.provider import LLMClient
from app.main import app
from app.observability import service
from app.observability.context import trace_context, trace_scope
from app.observability.recorder import record
from app.observability.sdk import observe_sdk_client
from app.rag.retriever import ExerciseRetriever
from app.config import RetrievalSettings
from app.utils.background import drain
from tests.test_agent import EchoTool
from tests.test_integration import make_user
from tests.test_rag import make_docs


def completion(usage=True):
    return NS(usage=NS(prompt_tokens=100, completion_tokens=50) if usage else None,
              choices=[NS(message=NS(content="回答"))])


def make_client(create):
    # Exercise the real instrumentation without contacting a provider.
    client = object.__new__(LLMClient)
    client.profile = settings.llm.normal
    client.tier = "normal"
    client._client = NS(chat=NS(completions=NS(create=create)))
    return client


async def rows(model, rid):
    async with SessionLocal() as db:
        return (await db.execute(select(model).where(model.request_id == rid))).scalars().all()


async def test_complete_stream_usage_errors_and_early_close(db):
    rid = new_id()
    async def create(**kwargs):
        return completion()
    with trace_scope(request_id=rid, user_id="u", session_id="s"):
        await make_client(create).complete_text([])
        async def broken(**kwargs):
            raise TimeoutError("sensitive provider response")
        with pytest.raises(TimeoutError):
            await make_client(broken).complete([])

        class Stream:
            closed = False
            def __aiter__(self):
                return self.chunks()
            async def chunks(self):
                yield NS(usage=None, choices=[NS(delta=NS(content=None, tool_calls=None))])
                yield NS(usage=None, choices=[NS(delta=NS(content="hi", tool_calls=None))])
                yield NS(usage=NS(prompt_tokens=200, completion_tokens=30), choices=[])
            async def close(self):
                self.closed = True
        stream = Stream()
        async def streaming(**kwargs):
            return stream
        client = make_client(streaming)
        assert len([c async for c in client.stream([])]) == 3
        assert stream.closed
        early = client.stream([])
        await anext(early)
        await early.aclose()
    await drain()
    calls = await rows(LLMCall, rid)
    assert len(calls) == 4
    assert sorted(c.status for c in calls) == ["cancelled", "error", "success", "success"]
    full_stream = next(c for c in calls if c.prompt_tokens == 200)
    assert full_stream.completion_tokens == 30 and full_stream.first_token_ms is not None
    assert next(c for c in calls if c.status == "error").error == "TimeoutError"
    assert all(c.user_id == "u" and c.session_id == "s" for c in calls)


async def test_stream_creation_failure_and_missing_usage(db):
    rid = new_id()
    async def broken(**kwargs):
        raise ConnectionError("secret")
    async def no_usage(**kwargs):
        return completion(False)
    with trace_scope(request_id=rid):
        with pytest.raises(ConnectionError):
            _ = [c async for c in make_client(broken).stream([])]
        await make_client(no_usage).complete([])
    await drain()
    calls = await rows(LLMCall, rid)
    assert len(calls) == 2 and all(c.prompt_tokens is None for c in calls)
    assert service.llm_summary(calls)["missing_usage_calls"] == 2


async def test_context_isolation_background_tools_and_retrieval(db):
    retriever = ExerciseRetriever(make_docs(), RetrievalSettings())
    executor = ToolExecutor([EchoTool()])
    rids = [new_id(), new_id()]
    async def worker(i):
        with trace_scope(request_id=rids[i], user_id=f"u{i}", session_id=f"s{i}"):
            await asyncio.sleep(0)
            await retriever.search("卧推")  # Missing dense channel must count as degraded.
            await executor.execute(ToolCall("1", "echo", {"text": "ok"}), ToolContext(f"u{i}", f"s{i}"))
            await executor.execute(ToolCall("2", "echo", {}), ToolContext(f"u{i}", f"s{i}"))
    await asyncio.gather(worker(0), worker(1))
    await drain()
    for i, rid in enumerate(rids):
        retrieval = (await rows(RetrievalLog, rid))[0]
        assert retrieval.user_id == f"u{i}" and retrieval.degraded and retrieval.hit_ids
        tools = await rows(ToolCallLog, rid)
        assert len(tools) == 2 and sum(t.success for t in tools) == 1
        assert all(t.session_id == f"s{i}" for t in tools)


async def test_sdk_eval_module_and_nested_context_reset(db):
    rid = new_id()
    async def create(**kwargs):
        return completion()
    client = NS(chat=NS(completions=NS(create=create)))
    observe_sdk_client(client, "evaluation")
    with trace_scope(request_id=rid, user_id="u", module="agent"):
        await client.chat.completions.create(model="judge")
        assert trace_context.get().module == "agent"
    await drain()
    call = (await rows(LLMCall, rid))[0]
    assert call.module == "evaluation" and call.prompt_tokens == 100


async def test_stats_cost_percentiles_access_control_and_request_trace(db, monkeypatch):
    alice, bob, admin = await make_user(), await make_user(), await make_user()
    rid_a, rid_b = new_id(), new_id()
    monkeypatch.setattr(settings.observability, "admin_user_ids", [admin])
    monkeypatch.setattr(settings.observability, "model_prices", {"priced": ModelPrice(input_per_million=2, output_per_million=4)})
    for uid, rid in [(alice, rid_a), (bob, rid_b)]:
        with trace_scope(request_id=rid, user_id=uid):
            for n in [10, 100]:
                record(LLMCall, module="agent", model="priced", prompt_tokens=1_000_000,
                       completion_tokens=500_000, latency_ms=n, first_token_ms=n / 2, status="success")
            record(ToolCallLog, tool_name="echo", success=False, error="bad", latency_ms=2)
            record(RetrievalLog, query="胸部", filters={}, mode="sparse_only", candidates=2,
                   hit_ids=["press"], latency_ms=5, degraded=True)
    await drain()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        def auth(uid):
            return {"Authorization": "Bearer " + create_access_token(uid)}
        assert (await client.get('/api/v1/stats/llm')).status_code == 401
        response = await client.get('/api/v1/stats/llm?range=7d', headers=auth(alice))
        assert response.status_code == 200
        data = response.json()
        assert data['summary']['calls'] == 2
        assert data['summary']['estimated_cost'] == 8
        assert data['summary']['latency_ms']['p50'] == 55
        assert data['summary']['latency_ms']['p95'] == 95.5
        assert data['tools']['failure_rate'] == 1
        assert response.headers['x-request-id']
        for path in ['llm', 'retrieval', 'requests', f'requests/{rid_b}']:
            assert (await client.get(f'/api/v1/stats/{path}?scope=all', headers=auth(alice))).status_code == 403
        assert (await client.get(f'/api/v1/stats/requests/{rid_b}', headers=auth(alice))).status_code == 404
        detail = (await client.get(f'/api/v1/stats/requests/{rid_a}', headers=auth(alice))).json()
        assert len(detail['llm_calls']) == 2 and len(detail['tool_calls']) == 1 and len(detail['retrieval_logs']) == 1
        assert (await client.get('/api/v1/stats/llm?range=bogus', headers=auth(alice))).status_code == 422
        global_data = (await client.get('/api/v1/stats/llm?scope=all', headers=auth(admin))).json()
        assert global_data['is_admin'] and global_data['summary']['calls'] >= 4
        retrieval = (await client.get('/api/v1/stats/retrieval', headers=auth(alice))).json()
        assert retrieval['degraded_count'] == 1
        items = (await client.get('/api/v1/stats/requests', headers=auth(alice))).json()
        assert {r['request_id'] for r in items} == {rid_a}


async def test_telemetry_write_failure_does_not_change_response(monkeypatch):
    def broken():
        raise RuntimeError("database unavailable")
    monkeypatch.setattr('app.database.session.SessionLocal', broken)
    async def create(**kw):
        return completion()
    with trace_scope(request_id=new_id()):
        assert await make_client(create).complete_text([]) == '回答'
    await drain()


def test_offline_report_missing_malformed_and_aggregate_only(tmp_path, monkeypatch):
    import json
    path = tmp_path / 'report.json'
    monkeypatch.setattr(settings.observability, 'offline_report_path', str(path))
    assert service.offline_report()['status'] == 'missing'
    path.write_text('bad json')
    assert service.offline_report()['status'] == 'invalid'
    path.write_text(json.dumps({'metadata': {'created_at': '2026-11-16T00:00:00Z'}, 'summary': {'hybrid': {'recall@5': .9}}, 'cases': ['private']}))
    report = service.offline_report()
    assert report['summary']['hybrid']['recall@5'] == .9 and 'private' not in str(report)


async def test_chat_request_id_connects_agent_tools_retrieval_and_background_memory(db, monkeypatch):
    from app.rag import retriever as retriever_module
    from tests.test_agent import chunk, tc
    uid = await make_user()
    streams = [
        [chunk(tool_calls=[tc(0, "c1", "search_exercises", '{"query":"卧推"}')])],
        [chunk("可选择杠铃卧推。")],
    ]
    class Stream:
        def __init__(self, chunks):
            self.chunks = chunks
        async def __aiter__(self):
            for c in self.chunks:
                yield c
            yield NS(usage=NS(prompt_tokens=80, completion_tokens=10), choices=[])
        async def close(self):
            pass
    async def create(**kw):
        if kw.get('stream'):
            return Stream(streams.pop(0))
        return NS(usage=NS(prompt_tokens=20, completion_tokens=5), choices=[NS(message=NS(content='{"operations": []}'))])
    llm = make_client(create)
    monkeypatch.setattr('app.agent.service.get_llm', lambda tier: llm)
    monkeypatch.setattr('app.memory.extractor.get_llm', lambda tier: llm)
    monkeypatch.setattr(settings.evaluation, 'enabled', False)
    monkeypatch.setattr(settings.memory, 'auto_extract', True)
    monkeypatch.setattr(retriever_module, '_retriever', ExerciseRetriever(make_docs(), RetrievalSettings()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/v1/agent/chat', json={'message': '推荐卧推'},
                                     headers={'Authorization': 'Bearer ' + create_access_token(uid)})
    assert response.status_code == 200
    rid = response.headers['x-request-id']
    assert f'"request_id": "{rid}"' in response.text
    await drain()
    detail = await service.request_detail(uid, 'self', rid)
    assert len(detail['llm_calls']) == 3
    assert {r['module'] for r in detail['llm_calls']} == {'agent', 'memory'}
    assert len(detail['tool_calls']) == 1 and len(detail['retrieval_logs']) == 1
    sid = detail['llm_calls'][0]['session_id']
    assert sid and all(r['session_id'] == sid for key in service.TABLES for r in detail[key])


async def test_no_candidate_search_is_not_a_service_degradation(db):
    from app.rag.schema import ExerciseFilter
    rid = new_id()
    retriever = ExerciseRetriever(make_docs(), RetrievalSettings())
    with trace_scope(request_id=rid):
        result = await retriever.search('胸部', ExerciseFilter(primary_muscles=['neck']))
    await drain()
    assert not result.hits
    log = (await rows(RetrievalLog, rid))[0]
    assert log.status == 'success' and not log.degraded
