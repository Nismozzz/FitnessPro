from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.rag.schema import ExerciseDoc
from app.training import progression, service
from app.training.progression import ApplyNextWeek, analyze_exercise
from app.training.schema import ExercisePrescription, LogCreate, PlanDraft
from tests.test_training import DOC, client_for, draft, training  # noqa: F401


def sessions(reps=10, weight=40, completed=True):
    return [{"date": date(2026, 11, 16) + timedelta(days=i * 7), "sets": [
        {"set_no": n, "reps": reps, "weight_kg": weight, "duration_seconds": None, "completed": completed}
        for n in range(1, 4)]} for i in range(3)]


def prescription(**kwargs):
    return ExercisePrescription(exercise_id="press", sets=3, reps_min=8, reps_max=10, target_weight_kg=40, **kwargs)


@pytest.mark.parametrize('muscles,increment', [(['chest'], 2.5), (['quadriceps', 'glutes'], 5)])
def test_three_weeks_overload(muscles, increment):
    result = analyze_exercise(prescription(), {**DOC, 'primary_muscles': muscles}, sessions(), date(2026, 12, 7))
    assert result['kind'] == 'increase_weight'
    assert result['changes']['target_weight_kg'] == 40 + increment


def test_three_weeks_deload_and_stagnation():
    assert analyze_exercise(prescription(), DOC, sessions(6), date(2026, 12, 7))['changes'] == {'target_weight_kg': 37}
    result = analyze_exercise(prescription(), DOC, sessions(9), date(2026, 12, 7))
    assert result['kind'] == 'replace'
    assert len(result['evidence']['weekly_estimated_1rm']) == 3
    improving = sessions(9)
    improving[-1]['sets'][0]['weight_kg'] = 45
    assert analyze_exercise(prescription(), DOC, improving, date(2026, 12, 7)) is None


def test_missing_partial_unknown_and_incompatible_records():
    p = prescription()
    for rows in [sessions()[:1], sessions(9)[1:], sessions(12, weight=None), sessions(12, weight=30)]:
        assert analyze_exercise(p, DOC, rows, date(2026, 12, 7)) is None
    rows = sessions()[-2:]
    for row in rows:
        row['sets'].pop()
    assert analyze_exercise(p, DOC, rows, date(2026, 12, 7)) is None
    rows = sessions()[-2:]
    for row in rows:
        row['prescription'] = {**p.model_dump(), 'sets': 4}
    assert analyze_exercise(p, DOC, rows, date(2026, 12, 7)) is None
    # Multiple records on one date do not constitute two separate training dates.
    rows = sessions()[:1] * 2
    assert analyze_exercise(p, DOC, rows, date(2026, 12, 7)) is None
    p = ExercisePrescription(exercise_id='press', sets=3, reps_min=8, reps_max=10)
    assert analyze_exercise(p, {**DOC, 'equipment': 'body only'}, sessions(weight=None), date(2026, 12, 7))['changes'] == {'reps_min': 9, 'reps_max': 11}
    p = ExercisePrescription(exercise_id='press', sets=3, duration_seconds=30)
    assert analyze_exercise(p, DOC, sessions(), date(2026, 12, 7)) is None


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 12, 7, 12, tzinfo=timezone.utc).astimezone(tz)


async def seed(training, monkeypatch, reps=9):
    uid, bob, ids = training
    monkeypatch.setattr(progression, 'datetime', FrozenDatetime)
    data = draft(ids[0], week='2026-11-30')
    for day in data['days']:
        day['exercises'][0].update(target_weight_kg=40, reps_max=10)
    plan = await service.save_plan(uid, PlanDraft.model_validate(data))
    await service.activate_plan(uid, plan['id'])
    for i, row in enumerate(sessions(reps)):
        await service.log_workout(uid, LogCreate.model_validate({
            'idempotency_key': f'progression-{i}', 'performed_at': str(row['date']) + 'T19:00:00+08:00',
            'duration_min': 40, 'sets': [{'exercise_id': ids[0], **s} for s in row['sets']]}))
    return uid, bob, ids, plan


async def test_suggest_apply_versions_idempotency_and_isolation(training, monkeypatch):
    uid, bob, ids, plan = await seed(training, monkeypatch, reps=10)
    async with await client_for(uid) as client:
        response = await client.post(f"/api/v1/plans/{plan['id']}/suggest-next-week")
        assert response.status_code == 200, response.text
        suggestion = response.json()
        assert {a['kind'] for a in suggestion['adjustments']} == {'increase_weight'}
        assert (await service.active_plan(uid))['id'] == plan['id']
        body = {'suggestion_id': suggestion['suggestion_id']}
        saved = await client.post(f"/api/v1/plans/{plan['id']}/next-week", json=body)
        assert saved.status_code == 201, saved.text
        new = saved.json()
        assert new['version'] == 2 and new['root_id'] == plan['root_id']
        assert new['status'] == 'draft' and new['week_start'] == '2026-12-07'
        assert new['days'][0]['exercises'][0]['target_weight_kg'] == 42.5
        assert (await client.post(f"/api/v1/plans/{plan['id']}/next-week", json=body)).json()['id'] == new['id']
        assert (await service.active_plan(uid))['id'] == plan['id']
        await service.activate_plan(uid, new['id'])
        assert (await service.get_plan(uid, plan['id']))['status'] == 'archived'
    async with await client_for(bob) as client:
        for suffix, body in [('suggest-next-week', None), ('next-week', {'suggestion_id': suggestion['suggestion_id']})]:
            result = await client.post(f"/api/v1/plans/{plan['id']}/{suffix}", json=body)
            assert result.status_code == 404


async def test_stagnation_retrieves_and_confirms_candidate(training, monkeypatch):
    uid, _, ids, plan = await seed(training, monkeypatch)
    alternative = ExerciseDoc(id=ids[2], name='Alternative Press', name_zh='替代卧推', equipment='dumbbell',
                              category='strength', level='beginner', primary_muscles=['chest'], secondary_muscles=[], instructions=[])
    # Persist candidate consistent with the index.
    from app.database.models import Exercise
    from app.database.session import SessionLocal
    async with SessionLocal() as db:
        row = await db.get(Exercise, ids[2]); row.equipment = 'dumbbell'
        await db.commit()
    calls = []
    class Retriever:
        async def search(self, query, filters, top_k):
            calls.append(filters)
            return SimpleNamespace(hits=[SimpleNamespace(doc=alternative)])
    monkeypatch.setattr(progression, 'get_retriever', lambda: Retriever())
    result = await progression.suggest_next_week(uid)
    assert calls and calls[0].equipment == ['dumbbell'] and calls[0].primary_muscles == ['chest']
    assert result['adjustments'][0]['candidates'][0]['exercise_id'] == ids[2]
    choice = {'weekday': 0, 'position': 0, 'exercise_id': ids[2]}
    body = ApplyNextWeek(suggestion_id=result['suggestion_id'], replacements=[choice])
    saved = await progression.apply_next_week(uid, plan['id'], body)
    assert saved['days'][0]['exercises'][0]['exercise_id'] == ids[2]
    assert saved['days'][0]['exercises'][0]['target_weight_kg'] is None
    with pytest.raises(service.TrainingError, match='候选'):
        await progression.apply_next_week(uid, plan['id'], ApplyNextWeek(suggestion_id=result['suggestion_id'], replacements=[{**choice, 'exercise_id': ids[1]}]))
    await service.log_workout(uid, LogCreate.model_validate({
        'idempotency_key': 'changed-record', 'performed_at': '2026-12-06T19:00:00+08:00', 'duration_min': 30,
        'sets': [{'exercise_id': ids[0], 'set_no': 1, 'reps': 9, 'weight_kg': 40}]}))
    with pytest.raises(service.TrainingError, match='已变化'):
        await progression.apply_next_week(uid, plan['id'], body)


async def test_knowledge_not_ready_keeps_original(training, monkeypatch):
    uid, _, _, _ = await seed(training, monkeypatch)
    def unavailable():
        raise progression.KnowledgeBaseNotReady('not ready')
    monkeypatch.setattr(progression, 'get_retriever', unavailable)
    result = await progression.suggest_next_week(uid)
    assert result['warnings'] and not result['adjustments'][0]['candidates']


async def test_agent_tools_preview_then_apply_use_server_identity(training, monkeypatch):
    from app.agent.tools.base import ToolContext, ToolExecutor
    from app.agent.tools.registry import build_default_tools
    from app.agent.types import ToolCall
    uid, bob, _, plan = await seed(training, monkeypatch, reps=6)
    executor = ToolExecutor(build_default_tools())
    context = ToolContext(uid, 'test')
    preview = await executor.execute(ToolCall(id='preview', name='suggest_next_week', arguments={}), context)
    assert preview.success and 'proposed_plan' not in preview.data
    assert preview.data['adjustments'][0]['kind'] == 'deload'
    args = {'plan_id': plan['id'], 'suggestion_id': preview.data['suggestion_id']}
    saved = await executor.execute(ToolCall(id='save', name='apply_next_week', arguments=args), context)
    assert saved.success and saved.data['version'] == 2
    assert saved.data['days'][0]['exercises'][0]['target_weight_kg'] == 37
    denied = await executor.execute(ToolCall(id='other', name='apply_next_week', arguments=args), ToolContext(bob, 'other'))
    assert not denied.success
    injected = await executor.execute(ToolCall(id='injected', name='suggest_next_week', arguments={'user_id': uid}), ToolContext(bob, 'other'))
    assert not injected.success


async def test_future_records_do_not_drive_progression(training, monkeypatch):
    uid, _, ids, plan = await seed(training, monkeypatch, reps=10)
    # Move the clock before the seeded training; future logs are excluded entirely.
    class EarlierDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 14, 12, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(progression, 'datetime', EarlierDatetime)
    result = await progression.suggest_next_week(uid)
    assert not result['adjustments']
