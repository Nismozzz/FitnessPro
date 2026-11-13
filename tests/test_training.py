import asyncio
import copy
import json
from datetime import date

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.agent.loop import AgentLoop
from app.agent.tools.base import ToolContext, ToolExecutor
from app.agent.tools.plan_tools import DraftWeeklyPlanTool, SavePlanTool
from app.auth.security import create_access_token
from app.config import AgentSettings
from app.database.models import Exercise, WorkoutPlan, new_id
from app.database.session import SessionLocal
from app.main import app
from app.memory.service import memory_service
from app.training.equipment import effective_equipment
from app.training.schema import PlanDraft
from app.training.service import check_plan
from app.training.stats import epley
from app.training.validator import muscle_credits, validate_plan
from app.users.profile import ProfileData, get_profile, update_profile
from tests.test_agent import FakeLLM, chunk, tc
from tests.test_integration import make_user

PROFILE = {"goal": "muscle_gain", "experience_level": "beginner", "training_days_per_week": 3,
           "session_minutes": 60, "training_location": "home", "equipment": ["dumbbell"]}
DOC = {"id": "press", "name": "Dumbbell Bench Press", "name_zh": "哑铃卧推", "equipment": "dumbbell",
       "level": "beginner", "category": "strength", "primary_muscles": ["chest"], "secondary_muscles": ["triceps", "shoulders"]}


def draft(eid="press", days=(0, 2, 4), week="2026-11-16"):
    return {"name": "全身训练", "goal": "muscle_gain", "week_start": week, "days": [
        {"weekday": d, "focus": "力量", "exercises": [{"exercise_id": eid, "sets": 3, "reps_min": 8, "reps_max": 12}]} for d in days]}


def validate(data=None, profile=None, docs=None):
    return validate_plan(PlanDraft.model_validate(data or draft()), profile or PROFILE, docs or {"press": DOC})


def codes(result, key="errors"):
    return {i["code"] for i in result[key]}


@pytest.mark.parametrize("profile,expected", [
    ({"training_location": "gym"}, {"barbell", "dumbbell", "cable", "machine", "body only"}),
    ({"training_location": "home", "equipment": ["dumbbell"]}, {"dumbbell", "body only"}),
    ({"training_location": "bodyweight", "equipment": ["barbell"]}, {"body only"}),
    ({"training_location": "custom", "equipment": ["bands"]}, {"bands", "body only"}),
    ({"equipment": []}, {"body only"}),
])
def test_scenario_equipment(profile, expected):
    assert expected <= effective_equipment(profile)


def test_unknown_is_not_all_equipment_and_exclusions_override():
    assert effective_equipment({}) is None
    assert "cable" not in effective_equipment({"training_location": "gym", "excluded_equipment": ["cable"]})
    assert effective_equipment({"training_location": "bodyweight", "excluded_equipment": ["body only"]}) == set()


@pytest.mark.parametrize("days,valid", [((0, 2, 4), True), ((0, 1), False), ((0, 6), False), ((1, 6), True), ((0,), True)])
def test_recovery_including_week_boundary(days, valid):
    assert ("recovery" not in codes(validate(draft(days=days)))) == valid


def test_rule_errors_and_soft_warnings():
    assert validate()["valid"]
    assert "equipment" in codes(validate(profile={**PROFILE, "equipment": ["bands"]}))
    assert "difficulty" in codes(validate(docs={"press": {**DOC, "level": "expert"}}))
    assert "training_days" in codes(validate(profile={**PROFILE, "training_days_per_week": 2}))
    long_plan = draft(); long_plan["days"][0]["exercises"][0]["sets"] = 10
    assert "duration" in codes(validate(long_plan, profile={**PROFILE, "session_minutes": 15}))
    assert "unknown_exercise" in codes(validate(draft(eid="missing")))
    assert "missing_equipment" in codes(validate(profile={k: v for k, v in PROFILE.items() if k not in {"training_location", "equipment"}}))
    assert "missing_profile" in codes(validate(profile={"training_location": "gym"}))
    data = draft(days=(0,)); data["days"][0]["exercises"][0].update(reps_min=3, reps_max=5)
    result = validate(data)
    assert result["valid"] and {"weekly_volume", "rep_range"} <= codes(result, "warnings")
    result = validate(profile={**PROFILE, "injuries": ["肩部疼痛"]})
    assert "injury_risk" in codes(result, "warnings")


def test_credits_do_not_double_count_same_group():
    assert muscle_credits({"primary_muscles": ["lats", "middle back"], "secondary_muscles": ["lats", "biceps"]}) == {"背部": 1, "肱二头肌": .5}


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(week_start="2026-11-17"),
    lambda d: d["days"].append(copy.deepcopy(d["days"][0])),
    lambda d: d["days"][0]["exercises"][0].update(reps_min=20, reps_max=8),
    lambda d: d["days"][0]["exercises"][0].update(sets=0),
    lambda d: d["days"][0]["exercises"][0].update(duration_seconds=30),
    lambda d: d["days"][0]["exercises"].append(copy.deepcopy(d["days"][0]["exercises"][0])),
])
def test_invalid_plan_schema(mutate):
    data = draft(); mutate(data)
    with pytest.raises(ValidationError):
        PlanDraft.model_validate(data)


def test_timed_plan_and_1rm_limits():
    data = draft(days=(0,))
    data["days"][0]["exercises"] = [{"exercise_id": "press", "sets": 2, "duration_seconds": 30}]
    assert validate(data)["valid"]
    assert epley(60, 8, "barbell") == 76
    assert epley(60, 1, "barbell") == 60
    for args in [(0, 8, "barbell"), (60, 20, "barbell"), (60, 8, "body only"), (60, 8, "machine")]:
        assert epley(*args) is None


@pytest.fixture
async def training(db):
    uid, bob = await make_user(), await make_user()
    prefix = new_id()[:10]
    ids = [prefix + x for x in ("press", "row", "barbell", "plank")]
    docs = [DOC, {**DOC, "name": "Dumbbell Row", "name_zh": "哑铃划船", "primary_muscles": ["lats"]},
            {**DOC, "name": "Barbell Press", "equipment": "barbell"},
            {**DOC, "name": "Plank", "equipment": "body only", "primary_muscles": ["abdominals"]}]
    async with SessionLocal() as dbs:
        for eid, doc in zip(ids, docs):
            fields = {k: v for k, v in doc.items() if k != "id"}
            dbs.add(Exercise(id=eid, instructions=["test"], **fields))
        await dbs.commit()
    await update_profile(uid, ProfileData(**PROFILE))
    return uid, bob, ids


async def client_for(uid):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                             headers={"Authorization": f"Bearer {create_access_token(uid)}"})


async def test_plan_save_activate_replace_version_and_idempotency(training):
    uid, _, ids = training
    async with await client_for(uid) as client:
        data = draft(ids[0])
        first = await client.post('/api/v1/plans', json=data)
        assert first.status_code == 201, first.text
        plan = first.json()
        assert plan['status'] == 'draft'
        again = await client.post('/api/v1/plans', json=data)
        assert again.json()['id'] == plan['id']
        assert (await client.post(f"/api/v1/plans/{plan['id']}/activate")).status_code == 200
        old_day = plan['days'][0]['id']
        prescription = {"exercise_id": ids[1], "sets": 3, "reps_min": 8, "reps_max": 12}
        replacement = await client.patch(f"/api/v1/plans/{plan['id']}/exercises", json={"weekday": 0, "position": 0, "exercise": prescription})
        assert replacement.status_code == 200, replacement.text
        newer = replacement.json()
        assert newer['status'] == 'draft' and newer['version'] == 2 and newer['id'] != plan['id']
        assert newer['days'][0]['id'] != old_day
        assert (await client.get('/api/v1/plans/active')).json()['id'] == plan['id']
        assert (await client.get(f"/api/v1/plans/{plan['id']}")).json()['days'][0]['exercises'][0]['exercise_id'] == ids[0]
        await client.post(f"/api/v1/plans/{newer['id']}/activate")
        assert (await client.get(f"/api/v1/plans/{plan['id']}")).json()['status'] == 'archived'
        different_week = await client.post('/api/v1/plans', json=draft(ids[0], week='2026-11-23'))
        assert different_week.json()['id'] != plan['id']


async def test_activate_revalidates_and_one_active_under_concurrency(training):
    uid, _, ids = training
    async with await client_for(uid) as client:
        plans = [(await client.post('/api/v1/plans', json=draft(ids[0], week=w))).json() for w in ['2026-11-16', '2026-11-23']]
        results = await asyncio.gather(*(client.post(f"/api/v1/plans/{p['id']}/activate") for p in plans))
        assert all(r.status_code == 200 for r in results)
        rows = (await client.get('/api/v1/plans')).json()
        assert sum(r['status'] == 'active' for r in rows) == 1
        await update_profile(uid, ProfileData(training_location='bodyweight'))
        response = await client.post(f"/api/v1/plans/{plans[0]['id']}/activate")
        assert response.status_code == 422 and response.json()['validation']['errors'][0]['code'] == 'equipment'


async def test_ownership_every_plan_log_and_stats_route(training):
    uid, bob, ids = training
    async with await client_for(uid) as alice, await client_for(bob) as other:
        plan = (await alice.post('/api/v1/plans', json=draft(ids[0]))).json()
        body = {"idempotency_key": new_id(), "plan_day_id": plan['days'][0]['id'], "performed_at": "2026-11-16T19:00:00+08:00", "duration_min": 30,
                "sets": [{"exercise_id": ids[0], "set_no": 1, "reps": 8, "weight_kg": 20}]}
        log = (await alice.post('/api/v1/logs', json=body)).json()
        for method, path, data in [('GET', f"/plans/{plan['id']}", None), ('POST', f"/plans/{plan['id']}/activate", None),
                                  ('PATCH', f"/plans/{plan['id']}/exercises", {"weekday": 0, "position": 0, "exercise": {"exercise_id": ids[1], "sets": 3, "reps_min": 8, "reps_max": 12}}),
                                  ('GET', f"/logs/{log['id']}", None), ('POST', '/logs', body),
                                  ('GET', f"/stats/weekly?week_start=2026-11-16&plan_id={plan['id']}", None)]:
            response = await other.request(method, '/api/v1' + path, json=data)
            assert response.status_code == 404, response.text
        assert (await other.get('/api/v1/plans')).json() == []
        assert (await other.get('/api/v1/logs')).json() == []
        assert (await other.get('/api/v1/stats/weekly?week_start=2026-11-16')).json()['workouts'] == 0


async def test_record_stats_idempotency_completion_timezone_and_pr(training):
    uid, _, ids = training
    async with await client_for(uid) as client:
        plan = (await client.post('/api/v1/plans', json=draft(ids[0], days=(0,)))).json()
        await client.post(f"/api/v1/plans/{plan['id']}/activate")
        body = {"idempotency_key": new_id(), "plan_day_id": plan['days'][0]['id'], "performed_at": "2026-11-16T00:30:00+08:00", "duration_min": 30,
                "sets": [{"exercise_id": ids[0], "set_no": i, "reps": 8, "weight_kg": 20, "completed": i < 3} for i in [1, 2, 3]]}
        first = await client.post('/api/v1/logs', json=body)
        assert first.status_code == 201, first.text
        again = await client.post('/api/v1/logs', json=body)
        assert again.json()['id'] == first.json()['id']
        wrong = await client.post('/api/v1/logs', json={**body, 'duration_min': 31})
        assert wrong.status_code == 409
        stats = (await client.get('/api/v1/stats/weekly?week_start=2026-11-16')).json()
        assert stats['workouts'] == 1 and stats['completed_sets'] == 2
        assert stats['volume_kg_reps'] == 320 and stats['daily'][0]['volume_kg_reps'] == 320
        assert stats['completion_rate'] == 0
        assert stats['prs'][0]['is_pr']
        body['idempotency_key'] = new_id()
        body['sets'] = [{"exercise_id": ids[0], "set_no": 3, "reps": 8, "weight_kg": 20}]
        await client.post('/api/v1/logs', json=body)
        stats = (await client.get('/api/v1/stats/weekly?week_start=2026-11-16')).json()
        assert stats['completion_rate'] == 1 and stats['completed_days'] == 1
        assert sum(m['volume_kg_reps'] for m in stats['muscles']) == stats['volume_kg_reps']
        await client.post('/api/v1/logs', json={**body, 'idempotency_key': new_id()})
        assert (await client.get('/api/v1/stats/weekly?week_start=2026-11-16')).json()['completion_rate'] == 1
        assert (await client.get('/api/v1/stats/weekly?week_start=2026-11-09')).json()['workouts'] == 0
        assert (await client.get('/api/v1/stats/weekly?week_start=2026-11-17')).status_code == 422


async def test_log_validation_and_timed_or_bodyweight_no_fake_volume(training):
    uid, _, ids = training
    async with await client_for(uid) as client:
        body = {'idempotency_key': new_id(), 'performed_at': '2026-11-16T19:00:00+08:00', 'duration_min': 30,
                'sets': [{'exercise_id': ids[3], 'set_no': 1, 'duration_seconds': 60, 'weight_kg': 60}]}
        assert (await client.post('/api/v1/logs', json=body)).status_code == 201
        stats = (await client.get('/api/v1/stats/weekly?week_start=2026-11-16')).json()
        assert stats['volume_kg_reps'] == 0 and stats['prs'] == [] and stats['completion_rate'] is None
        for entry in [{'exercise_id': 'missing', 'set_no': 1, 'reps': 8}, {'exercise_id': ids[0], 'set_no': 1}, {'exercise_id': ids[0], 'set_no': 1, 'reps': 8, 'duration_seconds': 30}, {'exercise_id': ids[0], 'set_no': 1, 'reps': 8, 'weight_kg': -1}]:
            assert (await client.post('/api/v1/logs', json={**body, 'idempotency_key': new_id(), 'sets': [entry]})).status_code == 422
        assert (await client.post('/api/v1/logs', json={**body, 'performed_at': '2026-11-16T19:00:00'})).status_code == 422


async def test_active_injury_memory_participates_in_validation(training):
    uid, _, ids = training
    await memory_service.add(uid, kind='injury', content='肩部有旧伤', status='active', source='manual')
    result = await check_plan(uid, PlanDraft.model_validate(draft(ids[0])))
    assert 'injury_risk' in codes(result, 'warnings')


async def test_fake_agent_validation_failure_correction_then_save(training):
    uid, _, ids = training
    invalid, valid = draft(ids[2]), draft(ids[0])
    def call(name, body, index):
        return chunk(tool_calls=[tc(0, f'call{index}', name, json.dumps(body))])
    llm = FakeLLM([[call('draft_weekly_plan', invalid, 1)], [call('draft_weekly_plan', valid, 2)], [call('save_plan', valid, 3)], [chunk('已保存草稿，请在计划页确认。')]])
    loop = AgentLoop(llm, ToolExecutor([DraftWeeklyPlanTool(), SavePlanTool()]), AgentSettings(max_iterations=6))
    transcript = []
    events = [e async for e in loop.run([{'role': 'user', 'content': '制定一周计划'}], ToolContext(uid, 'test'), transcript)]
    results = [e.data['data'] for e in events if e.type == 'tool_result']
    assert not results[0]['valid'] and 'equipment' in codes(results[0])
    assert results[1]['valid'] and results[2]['status'] == 'draft'
    assert events[-1].data['status'] == 'completed'
    async with SessionLocal() as db:
        rows = (await db.scalars(select(WorkoutPlan).where(WorkoutPlan.user_id == uid))).all()
        assert len(rows) == 1 and rows[0].status == 'draft'


async def test_profile_scene_normalization_and_legacy_equipment(training):
    uid, _, _ = training
    await update_profile(uid, ProfileData(training_location='gym', excluded_equipment=['龙门架']))
    profile = await get_profile(uid)
    assert profile['excluded_equipment'] == ['cable']
    assert 'cable' not in effective_equipment(profile) and 'machine' in effective_equipment(profile)


async def test_additive_upgrade_preserves_existing_profile_data(tmp_path, monkeypatch):
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.database import session
    legacy_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    async with legacy_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE user_profiles (user_id VARCHAR(32) PRIMARY KEY, notes TEXT)"))
        await conn.execute(text("INSERT INTO user_profiles (user_id, notes) VALUES ('legacy', '保留原档案')"))
    monkeypatch.setattr(session, 'engine', legacy_engine)
    try:
        await session.init_db()
        await session.init_db()  # Repeated startup must remain safe.
        async with legacy_engine.connect() as conn:
            columns = await conn.run_sync(lambda c: {x['name'] for x in inspect(c).get_columns('user_profiles')})
            assert {'training_location', 'excluded_equipment'} <= columns
            assert (await conn.execute(text("SELECT notes FROM user_profiles WHERE user_id='legacy'"))).scalar() == '保留原档案'
    finally:
        await legacy_engine.dispose()


async def test_api_rejects_unvalidated_plan_and_invalid_replacement_leaves_no_rows(training):
    uid, _, ids = training
    async with await client_for(uid) as client:
        invalid = await client.post('/api/v1/plans', json=draft(ids[2]))
        assert invalid.status_code == 422
        assert (await client.get('/api/v1/plans')).json() == []
        plan = (await client.post('/api/v1/plans', json=draft(ids[0]))).json()
        invalid = await client.patch(f"/api/v1/plans/{plan['id']}/exercises", json={
            'weekday': 0, 'position': 0, 'exercise': {'exercise_id': ids[2], 'sets': 3, 'reps_min': 8, 'reps_max': 12}})
        assert invalid.status_code == 422
        assert len((await client.get('/api/v1/plans')).json()) == 1


async def test_query_and_agent_search_share_profile_equipment(training, monkeypatch):
    from types import SimpleNamespace
    from app.api import knowledge
    from app.agent.tools import exercise_tools
    uid, _, _ = training
    await update_profile(uid, ProfileData(training_location='gym', excluded_equipment=['cable']))
    seen = []
    class Retriever:
        async def search(self, query, filters, top_k):
            seen.append(set(filters.equipment))
            return SimpleNamespace(total_candidates=0, mode='bm25', warnings=[], hits=[])
    retriever = Retriever()
    monkeypatch.setattr(knowledge, '_retriever', lambda: retriever)
    monkeypatch.setattr(exercise_tools, 'get_retriever', lambda: retriever)
    async with await client_for(uid) as client:
        assert (await client.post('/api/v1/knowledge/search', json={'query': '胸部'})).status_code == 200
    tool = exercise_tools.SearchExercisesTool()
    await tool.run(exercise_tools.SearchExercisesParams(query='胸部'), ToolContext(uid, 'test'))
    assert seen[0] == seen[1]
    assert 'cable' not in seen[0] and {'dumbbell', 'machine', 'body only'} <= seen[0]


@pytest.mark.parametrize('location,equipment,goal,reps', [
    ('gym', [], 'strength', (3, 6)),
    ('home', ['dumbbell'], 'muscle_gain', (8, 12)),
    ('bodyweight', [], 'endurance', (12, 20)),
    ('custom', ['bands'], 'general_fitness', (10, 15)),
])
def test_plan_acceptance_is_general_across_scenarios(location, equipment, goal, reps):
    doc_equipment = 'body only' if location == 'bodyweight' else 'bands' if location == 'custom' else 'dumbbell'
    profile = {**PROFILE, 'training_location': location, 'equipment': equipment, 'goal': goal}
    data = draft(); data['goal'] = goal
    for day in data['days']:
        day['exercises'][0].update(reps_min=reps[0], reps_max=reps[1])
    result = validate(data, profile, {'press': {**DOC, 'equipment': doc_equipment}})
    assert result['valid']
    assert 'rep_range' not in codes(result, 'warnings')


async def test_agent_record_and_stats_tools_use_real_storage_and_idempotency(training):
    from app.agent.tools.plan_tools import LogWorkoutTool, GetTrainingStatsTool, StatsParams
    from app.training.schema import LogCreate
    from app.training.service import list_logs
    uid, _, ids = training
    params = LogCreate.model_validate({'idempotency_key': new_id(), 'performed_at': '2026-11-16T19:00:00+08:00', 'duration_min': 30,
                                     'sets': [{'exercise_id': ids[0], 'set_no': 1, 'reps': 8, 'weight_kg': 20}]})
    tool, context = LogWorkoutTool(), ToolContext(uid, 'test')
    first = await tool.run(params, context)
    second = await tool.run(params, context)
    assert first['id'] == second['id'] and len(await list_logs(uid)) == 1
    stats = await GetTrainingStatsTool().run(StatsParams(week_start=date(2026, 11, 16)), context)
    assert stats['completed_sets'] == 1 and stats['volume_kg_reps'] == 160
