"""Imported only inside an isolated worker, after DATABASE_URL points at a new temp database."""
from __future__ import annotations

import time
from datetime import datetime, time as day_time, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select

from app.agent.loop import AgentLoop
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools.base import ToolContext, ToolExecutor
from app.agent.tools.registry import build_default_tools
from app.config import settings
from app.conversation import repository
from app.database.models import Exercise, User, new_id
from app.database.session import SessionLocal, init_db
from app.memory.service import memory_service, render_memories
from app.rag.retriever import ExerciseRetriever, set_retriever
from app.training import service
from app.training.schema import LogCreate, PlanDraft
from app.users.profile import ProfileData, get_profile, render_profile, update_profile
from app.utils.time import format_local
from eval.agent_scoring import score_agent

FIXTURE_EXERCISE = 'Dumbbell_Bench_Press'


def substitute(value, mapping):
    if isinstance(value, str):
        for key, replacement in mapping.items():
            value = value.replace('{{' + key + '}}', replacement)
        return value
    if isinstance(value, list):
        return [substitute(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, mapping) for k, v in value.items()}
    return value


async def setup_case(case, catalog, now):
    await init_db()
    uid = new_id()
    async with SessionLocal() as db:
        db.add(User(id=uid, username=f'eval_{uid}', password_hash='!evaluation-disabled-login'))
        db.add_all(Exercise(**row) for row in catalog)
        await db.commit()
    await update_profile(uid, ProfileData.model_validate(case.profile))
    session = await repository.create_session(uid, 'evaluation')
    monday = now.date() - timedelta(days=now.weekday())
    mapping = {'week_start': str(monday), 'next_week_start': str(monday + timedelta(days=7)),
               'performed_at': now.isoformat(timespec='seconds'), 'today': str(now.date()), 'plan_id': '', 'plan_day_id': '', 'memory_id': ''}
    if case.fixture in {'plan', 'logs'}:
        week = monday
        plan = PlanDraft.model_validate({'name': '评测基础计划', 'goal': case.profile.get('goal', 'muscle_gain'), 'week_start': str(week),
            'days': [{'weekday': d, 'focus': '胸部', 'exercises': [{'exercise_id': FIXTURE_EXERCISE, 'sets': 3,
                        'reps_min': 8, 'reps_max': 12, 'target_weight_kg': 40}]} for d in (0, 2, 4)]})
        saved = await service.save_plan(uid, plan)
        await service.activate_plan(uid, saved['id'])
        mapping.update(plan_id=saved['id'], plan_day_id=saved['days'][0]['id'])
        if case.fixture == 'logs':
            reps = 8
            days = [monday]
            for i, day in enumerate(days):
                when = datetime.combine(day, day_time(10), tzinfo=now.tzinfo)
                await service.log_workout(uid, LogCreate.model_validate({'idempotency_key': f'eval-seed-{i}', 'performed_at': when.isoformat(),
                    'duration_min': 30, 'sets': [{'exercise_id': FIXTURE_EXERCISE, 'set_no': n, 'reps': reps, 'weight_kg': 40} for n in (1, 2, 3)]}))
    if case.fixture in {'memory', 'candidate'}:
        memory, _ = await memory_service.add(uid, kind='preference', content='用户偏好哑铃训练',
                                             status='candidate' if case.fixture == 'candidate' else 'active', source='manual')
        mapping['memory_id'] = memory.id
    return uid, session.id, mapping


class TracingExecutor(ToolExecutor):
    def __init__(self, tools, case):
        super().__init__(tools, default_timeout=settings.agent.tool_timeout)
        self.trace, self.case = [], case
        self.searched_ids, self.grounding_checks = set(), []

    async def execute(self, call, ctx):
        # Check provenance at the time the plan is saved, not against later search results.
        if call.name == 'save_plan' and self.case.check_plan_grounding:
            ids = {e.get('exercise_id') for day in call.arguments.get('days', []) for e in day.get('exercises', [])}
            self.grounding_checks.append(bool(ids) and ids <= self.searched_ids)
        result = await super().execute(call, ctx)
        if call.name == 'search_exercises' and result.success:
            self.searched_ids.update(e['id'] for e in (result.data or {}).get('exercises', []))
            if self.case.tool_result_injection:
                result.data = {**result.data, 'untrusted_content': self.case.tool_result_injection}
        self.trace.append({'id': call.id, 'name': call.name, 'arguments': call.arguments,
                           'parse_error': call.parse_error, 'success': result.success, 'error': result.error, 'data': result.data})
        return result


async def run_case(case, catalog, now, *, llm=None, retrieval_mode='hybrid_rerank'):
    started = time.perf_counter()
    uid, sid, mapping = await setup_case(case, catalog, now)
    # Substitute only fixture placeholders, preserving the case schema and parameter expectations.
    case = type(case).model_validate(substitute(case.model_dump(), mapping))
    retriever = await ExerciseRetriever.load(enable_dense=retrieval_mode != 'bm25', enable_rerank=retrieval_mode == 'hybrid_rerank')
    if retrieval_mode != 'bm25' and retriever.vector_store is None:
        raise RuntimeError('dense retrieval unavailable; select --retrieval-mode bm25 explicitly for an offline run')
    if retrieval_mode == 'hybrid_rerank' and retriever.reranker is None:
        raise RuntimeError('reranker unavailable; choose an explicit non-rerank retrieval mode')
    set_retriever(retriever)
    executor = TracingExecutor(build_default_tools(), case)
    active, candidates = await memory_service.for_prompt(uid)
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT.format(now=format_local(now), profile=render_profile(await get_profile(uid)),
                 memories=render_memories(active, candidates, settings.context.memory_prompt_tokens), summary='')},
                {'role': 'user', 'content': case.prompt}]
    before_plans, before_logs = await service.list_plans(uid), await service.list_logs(uid)
    if llm is None:
        from app.llm.provider import get_llm
        llm = get_llm('normal')
    transcript, done, errors = [], {}, []
    # Keep the system prompt, current-time tool and fixture dates on the same clock.
    with patch('app.agent.tools.time_tool.local_now', lambda _: now):
        loop = AgentLoop(llm, executor, settings.agent, settings.context.tool_result_max_tokens)
        async for event in loop.run(messages, ToolContext(uid, sid), transcript):
            if event.type == 'done':
                done = event.data
            elif event.type == 'error':
                errors.append(event.data)
    final = next((m.get('content') or '' for m in reversed(transcript) if m['role'] == 'assistant' and not m.get('tool_calls')), '')
    plans, logs = await service.list_plans(uid), await service.list_logs(uid)
    new_plans = [p for p in plans if p['id'] not in {p['id'] for p in before_plans}]
    retrieval_degraded = any(any('向量检索暂不可用' in w or '重排服务暂不可用' in w for w in (t.get('data') or {}).get('warnings', []))
                             for t in executor.trace if t['name'] == 'search_exercises' and t['success'])
    state = {'retrieval_degraded': retrieval_degraded, 'new_plans': len(new_plans), 'new_logs': len(logs) - len(before_logs),
             'plan_grounded': bool(executor.grounding_checks) and all(executor.grounding_checks),
             'injury_risk_plan_ids': [p['id'] for p in new_plans if any(w['code'] == 'injury_risk' for w in p['validation']['warnings'])]}
    return {'case_id': case.id, 'category': case.category, 'prompt': case.prompt, 'done': done, 'errors': errors,
            'final_answer': final, 'trace': executor.trace, 'state': state, 'score': score_agent(case, executor.trace, final, done, state),
            'latency_ms': round((time.perf_counter() - started) * 1000, 3), 'retrieval_mode': retrieval_mode}
