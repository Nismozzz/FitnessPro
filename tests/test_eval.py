import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from app.config import RetrievalSettings
from app.rag.retriever import ExerciseRetriever
from app.rag.schema import ExerciseFilter
from app.rag.vector_store import LocalVectorStore
from eval.agent_scoring import aggregate_agent, score_agent
from eval.common import AgentCase, RetrievalCase, load_cases, retrieval_metrics
from scripts.eval_retrieval import aggregate, apply_judgments, build_pool, evaluate, validate_catalog
from tests.test_agent import FakeLLM, chunk, tc
from tests.test_rag import make_docs

ROOT = Path(__file__).resolve().parents[1]


def retrieval_case(**kwargs):
    return RetrievalCase(id='r1', query='卧推', category='alias', relevant=['Barbell_Bench_Press'], rationale='test', **kwargs)


def agent_case(**kwargs):
    return AgentCase(id='a1', category='test', prompt='test', rationale='test', **kwargs)


def trace(name, arguments=None, success=True, data=None):
    return {'name': name, 'arguments': arguments or {}, 'success': success, 'data': data}


STATE = {'new_plans': 0, 'new_logs': 0, 'plan_grounded': False, 'injury_risk_plan_ids': []}
DONE = {'status': 'completed', 'iterations': 2, 'usage': {'prompt_tokens': 100, 'completion_tokens': 50}}


def test_retrieval_metrics_known_ranking_and_no_answer_denominators():
    result = retrieval_metrics(['wrong', 'a', 'b'], ['a', 'b', 'c'], 3)
    assert result['recall'] == 2 / 3 and result['mrr'] == .5
    assert result['ndcg'] == pytest.approx((1 / math.log2(3) + .5) / (1 + 1 / math.log2(3) + .5))
    assert retrieval_metrics([], [], 5, True)['no_answer_correct'] is True
    assert retrieval_metrics(['irrelevant'], [], 5, True)['no_answer_correct'] is False
    assert retrieval_metrics([], ['a'], 5)['ndcg'] == 0
    with pytest.raises(ValueError):
        retrieval_metrics(['a', 'a'], ['a'], 5)


async def test_search_ablation_really_disables_independent_paths(monkeypatch):
    docs = make_docs()
    calls = {'embed': 0, 'sparse': 0, 'rerank': 0}
    class Embedder:
        async def embed_query(self, query):
            calls['embed'] += 1
            return np.array([1., 0.])
    class Reranker:
        async def rerank(self, query, texts, top_n):
            calls['rerank'] += 1
            return [(i, 1. - i / 10) for i in range(min(top_n, len(texts)))]
    store = LocalVectorStore([d.id for d in docs], np.array([[1, 0]] * len(docs)))
    r = ExerciseRetriever(docs, RetrievalSettings(), store, Embedder(), Reranker())
    def sparse(*args):
        calls['sparse'] += 1
        return [(d.id, 1) for d in docs]
    monkeypatch.setattr(r.bm25, 'search', sparse)
    assert (await r.search('胸', mode='bm25', rerank=False)).mode == 'sparse_only'
    assert calls == {'embed': 0, 'sparse': 1, 'rerank': 0}
    assert (await r.search('胸', mode='dense', rerank=False)).mode == 'dense_only'
    assert calls == {'embed': 1, 'sparse': 1, 'rerank': 0}
    assert (await r.search('胸', mode='hybrid', rerank=True)).mode == 'hybrid+rerank'
    assert calls == {'embed': 1, 'sparse': 2, 'rerank': 1}
    with pytest.raises(ValueError):
        await r.search('胸', mode='typo')


async def test_evaluation_excludes_unavailable_and_degraded_modes(monkeypatch):
    docs = make_docs()
    r = ExerciseRetriever(docs, RetrievalSettings())
    rows = await evaluate([retrieval_case()], r, ['bm25', 'dense'], 5)
    assert rows[1]['status'] == 'unavailable'
    assert aggregate(rows, 5)['scored_cases'] == 1
    class BrokenEmbedder:
        async def embed_query(self, query):
            raise RuntimeError('network unavailable')
    r = ExerciseRetriever(docs, RetrievalSettings(), LocalVectorStore([d.id for d in docs], np.ones((len(docs), 2))), BrokenEmbedder())
    rows = await evaluate([retrieval_case()], r, ['hybrid'], 5)
    assert rows[0]['status'] == 'degraded'
    assert aggregate(rows, 5)['recall@5'] is None


def test_pool_union_and_reviewed_judgments(tmp_path):
    docs = {d.id: d for d in make_docs()}
    case = retrieval_case()
    rows = [{'case_id': case.id, 'requested_mode': 'bm25', 'hit_ids': ['Barbell_Bench_Press'], 'status': 'ok'},
            {'case_id': case.id, 'requested_mode': 'dense', 'hit_ids': ['Dumbbell_Flyes'], 'status': 'ok'},
            {'case_id': case.id, 'requested_mode': 'hybrid', 'hit_ids': [], 'status': 'ok'},
            {'case_id': case.id, 'requested_mode': 'hybrid_rerank', 'hit_ids': [], 'status': 'ok'}]
    pool = build_pool([case], rows, docs)
    assert {c['exercise_id'] for c in pool[0]['candidates']} == {'Barbell_Bench_Press', 'Dumbbell_Flyes'}
    path = tmp_path / 'judgments.jsonl'
    path.write_text(json.dumps(pool[0]))
    with pytest.raises(ValueError, match='unreviewed'):
        apply_judgments([case], path, docs)
    pool[0]['reviewed'] = True
    for candidate in pool[0]['candidates']:
        candidate['relevance'] = 1
    path.write_text(json.dumps(pool[0]))
    apply_judgments([case], path, docs)
    assert case.label_status == 'reviewed' and len(case.relevant) == 2
    case.filters = {'equipment': ['cable']}
    with pytest.raises(ValueError, match='violates'):
        validate_catalog([case], docs)


def test_agent_scores_missing_wrong_arguments_order_and_failed_calls():
    case = agent_case(expected_tools=[{'name': 'search_exercises', 'arguments': {'equipment': ['dumbbell']}}],
                      forbidden_tools=['forget_memory'], ordered_tools=['search_exercises', 'save_plan'])
    good = [trace('search_exercises', {'equipment': ['body only', 'dumbbell']}), trace('save_plan')]
    assert score_agent(case, good, '已完成', DONE, STATE)['passed']
    assert not score_agent(case, [], '没有做', DONE, STATE)['passed']
    assert not score_agent(case, good[::-1], '已完成', DONE, STATE)['tool_order_correct']
    bad = [trace('search_exercises', {'equipment': ['cable']}), trace('save_plan')]
    assert not score_agent(case, bad, '已完成', DONE, STATE)['argument_correct']
    bad = [trace('search_exercises', {'equipment': ['dumbbell']}, success=False), trace('save_plan')]
    score = score_agent(case, bad, '已完成', DONE, STATE)
    assert not score['passed'] and not score['required_tools_succeeded']
    assert not score_agent(case, good + [trace('forget_memory')], '已完成', DONE, STATE)['passed']


def test_missing_plan_validation_and_infrastructure_failures_do_not_inflate_scores():
    case = agent_case(expect_plan_saved=True, check_plan_grounding=True)
    failed = score_agent(case, [], '已保存', DONE, STATE)
    assert failed['plan_first_pass_valid'] is False and not failed['passed']
    rows = [{'score': failed, 'done': DONE}, {'error': 'timeout'}]
    metrics = aggregate_agent(rows)
    assert metrics['cases'] == 2 and metrics['error_cases'] == 1 and metrics['pass_rate'] == 0
    assert metrics['average_total_tokens'] == 150
    case = agent_case(required_patterns=['医生|康复师'], forbidden_patterns=['保证治愈'])
    assert not score_agent(case, [], '保证治愈', DONE, STATE)['passed']


def test_committed_datasets_have_coverage_and_valid_tool_contracts():
    from scripts.eval_agent import validate_cases
    retrieval = load_cases(ROOT / 'eval/retrieval_cases.jsonl', RetrievalCase)
    agent = load_cases(ROOT / 'eval/agent_cases.jsonl', AgentCase)
    assert 50 <= len(retrieval) <= 100 and len(agent) >= 30
    assert {'no_answer', 'constraint', 'injury_constraint', 'semantic'} <= {c.category for c in retrieval}
    assert {'plan', 'logging', 'clarification', 'tool_injection', 'user_injection', 'memory'} <= {c.category for c in agent}
    validate_cases(agent, [{'id': 'Dumbbell_Bench_Press'}])


async def test_agent_runtime_runs_real_tools_and_scores_real_persistence(db):
    from eval.agent_runtime import run_case
    now = datetime.fromisoformat('2026-11-14T12:00:00+08:00')
    profile = {'goal': 'muscle_gain', 'experience_level': 'beginner', 'training_days_per_week': 3,
               'session_minutes': 60, 'training_location': 'home', 'equipment': ['dumbbell']}
    catalog = [{'id': 'Dumbbell_Bench_Press', 'name': 'Dumbbell Bench Press', 'name_zh': '哑铃卧推', 'equipment': 'dumbbell',
                'category': 'strength', 'level': 'beginner', 'primary_muscles': ['chest'], 'secondary_muscles': [], 'instructions': ['Press.']}]
    case = agent_case(profile=profile, expected_tools=[{'name': 'log_workout', 'arguments': {'duration_min': 30}}], expect_log_saved=True)
    llm = FakeLLM([[chunk(tool_calls=[tc(0, 'log', 'log_workout', json.dumps({
        'idempotency_key': 'test-evaluation-log', 'performed_at': now.isoformat(), 'duration_min': 30,
        'sets': [{'exercise_id': 'Dumbbell_Bench_Press', 'set_no': 1, 'reps': 8, 'weight_kg': 40}]}))])], [chunk(content='训练已记录。')]])
    result = await run_case(case, catalog, now, llm=llm, retrieval_mode='bm25')
    assert result['state']['new_logs'] == 1 and result['score']['passed']
    assert result['done']['iterations'] == 2
    assert result['trace'][0]['success']
    from app.rag.retriever import set_retriever
    set_retriever(None)


async def test_worker_processes_have_separate_databases_and_preserve_source(db, tmp_path):
    import os
    import subprocess
    import sys
    import sqlite3
    from app.database.models import User
    from app.database.session import SessionLocal
    from sqlalchemy import func, select
    async with SessionLocal() as source:
        before = await source.scalar(select(func.count()).select_from(User))
    catalog_path = tmp_path / 'empty-catalog.json'
    catalog_path.write_text('[]')
    database_paths = []
    for i in range(2):
        folder = tmp_path / str(i)
        folder.mkdir()
        payload_path, output_path = folder / 'input.json', folder / 'result.json'
        payload_path.write_text(json.dumps({'case': agent_case().model_dump(), 'catalog_path': str(catalog_path),
                                          'now': '2026-11-14T12:00:00+08:00', 'temperature': 0, 'model': None, 'retrieval_mode': 'bm25'}))
        target = folder / 'case.db'
        env = {**os.environ, 'DATABASE_URL': f'sqlite+aiosqlite:///{target}', 'FITNESSPRO_EVAL_WORKER': '1'}
        process = subprocess.run([sys.executable, '-m', 'scripts.eval_agent', '--worker', str(payload_path), '--worker-output', str(output_path)],
                                 cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
        assert process.returncode == 0, process.stderr
        # Empty catalog intentionally stops before any LLM call; worker errors remain reportable.
        assert json.loads(output_path.read_text())['error'] == 'KnowledgeBaseNotReady'
        with sqlite3.connect(target) as isolated:
            assert isolated.execute('select count(*) from users').fetchone()[0] == 1
        database_paths.append(target)
    assert database_paths[0] != database_paths[1]
    async with SessionLocal() as source:
        assert await source.scalar(select(func.count()).select_from(User)) == before
    # A worker mistakenly pointed at another database fails before creating it.
    wrong_target = tmp_path / 'must-not-create.db'
    env['DATABASE_URL'] = f'sqlite+aiosqlite:///{wrong_target}'
    process = subprocess.run([sys.executable, '-m', 'scripts.eval_agent', '--worker', str(payload_path), '--worker-output', str(output_path)],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
    assert json.loads(output_path.read_text())['error'] == 'RuntimeError'
    assert not wrong_target.exists()
