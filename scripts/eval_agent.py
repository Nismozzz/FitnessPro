"""Run isolated real AgentLoop cases. python -m scripts.eval_agent --help; see eval/PROTOCOL.md."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from eval.common import ROOT, AgentCase, load_cases, metadata, write_json, write_jsonl
from eval.agent_scoring import aggregate_agent


async def export_catalog():
    # Read only exercise rows from the configured source DB. Never init_db or create users here.
    from sqlalchemy import select
    from app.database.models import Exercise
    from app.database.session import SessionLocal
    async with SessionLocal() as db:
        rows = (await db.scalars(select(Exercise).order_by(Exercise.id))).all()
    return [{c.name: getattr(row, c.name) for c in Exercise.__table__.columns if c.name != 'updated_at'} for row in rows]


def validate_cases(cases, catalog):
    from app.agent.tools.registry import build_default_tools
    from app.users.profile import ProfileData
    tools = {tool.name: tool for tool in build_default_tools()}
    ids = {doc['id'] for doc in catalog}
    for case in cases:
        if set(case.profile) - set(ProfileData.model_fields):
            raise ValueError(f"{case.id}: unknown profile fields")
        ProfileData.model_validate(case.profile)
        for name in [e.name for e in case.expected_tools] + case.forbidden_tools + case.ordered_tools:
            if name not in tools:
                raise ValueError(f'{case.id}: unknown tool {name}')
        for e in case.expected_tools:
            if set(e.arguments) - set(tools[e.name].Params.model_fields):
                raise ValueError(f'{case.id}: unknown argument in {e.name}')
        if set(e.name for e in case.expected_tools) & set(case.forbidden_tools):
            raise ValueError(f'{case.id}: required tool is also forbidden')
        for pattern in case.required_patterns + case.forbidden_patterns:
            re.compile(pattern)
        if case.fixture in {'plan', 'logs'} and 'Dumbbell_Bench_Press' not in ids:
            raise ValueError(f'{case.id}: missing fixture exercise Dumbbell_Bench_Press')
    if not catalog:
        raise ValueError('exercise catalog is empty')


async def worker(payload_path, output_path):
    # Parent assigned a new temporary DATABASE_URL before Python imports any app module.
    if not os.environ.get('FITNESSPRO_EVAL_WORKER'):
        raise RuntimeError('worker must be started by eval_agent supervisor')
    payload = json.loads(payload_path.read_text(encoding='utf-8'))
    from app.config import settings
    from app.database.session import engine
    from eval.agent_runtime import run_case
    # Refuse an inherited production DB even if a worker was launched incorrectly.
    expected_db = payload_path.parent / 'case.db'
    if engine.url.database != str(expected_db):
        raise RuntimeError('worker database must be the per-case temporary database')
    settings.llm.normal.temperature = payload['temperature']
    if payload['model']:
        settings.llm.normal.model = payload['model']
    try:
        catalog = json.loads(Path(payload['catalog_path']).read_text(encoding='utf-8'))
        result = await run_case(AgentCase.model_validate(payload['case']), catalog, datetime.fromisoformat(payload['now']),
                                retrieval_mode=payload['retrieval_mode'])
        write_json(output_path, result)
    finally:
        await engine.dispose()


def run(args):
    from app.config import settings
    cases = load_cases(args.cases, AgentCase)
    if args.case_id:
        cases = [c for c in cases if c.id in args.case_id]
        if len(cases) != len(set(args.case_id)):
            raise ValueError('unknown case id')
    if args.limit:
        cases = cases[:args.limit]
    catalog = asyncio.run(export_catalog())
    validate_cases(cases, catalog)
    if args.validate_only:
        print(f'Validated {len(cases)} agent cases and fixtures; no API calls or source database writes.')
        return 0
    if not settings.llm.normal.api_key:
        raise ValueError('LLM API key is not configured')
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(ZoneInfo(settings.timezone))
    if now.tzinfo is None:
        raise ValueError('--now must include a timezone offset')
    now = now.astimezone(ZoneInfo(settings.timezone))
    args.output.mkdir(parents=True, exist_ok=True)
    config = {'model': args.model or settings.llm.normal.model, 'temperature': args.temperature,
              'max_tokens': settings.llm.normal.max_tokens, 'extra_body': settings.llm.normal.extra_body,
              'now': now.isoformat(), 'repeat': args.repeat, 'agent': settings.agent.model_dump(),
              'retrieval_mode': args.retrieval_mode, 'retrieval': settings.retrieval.model_dump(),
              'embedding_model': settings.embedding.model, 'reranker_model': settings.reranker.model,
              'tool_result_max_tokens': settings.context.tool_result_max_tokens}
    meta = metadata(args.cases, catalog, config)
    rows = []
    with tempfile.TemporaryDirectory(prefix='fitnesspro-eval-') as workspace:
        workspace = Path(workspace)
        catalog_path = workspace / 'catalog.json'
        write_json(catalog_path, catalog)
        for repeat in range(1, args.repeat + 1):
            for case in cases:
                with tempfile.TemporaryDirectory(prefix=f'{case.id}-', dir=workspace) as case_dir:
                    case_dir = Path(case_dir)
                    payload_path, output_path = case_dir / 'input.json', case_dir / 'result.json'
                    write_json(payload_path, {'case': case.model_dump(), 'catalog_path': str(catalog_path), 'now': now.isoformat(),
                                             'temperature': args.temperature, 'model': args.model, 'retrieval_mode': args.retrieval_mode})
                    env = {**os.environ, 'DATABASE_URL': f'sqlite+aiosqlite:///{case_dir / "case.db"}', 'FITNESSPRO_EVAL_WORKER': '1'}
                    started = time.perf_counter()
                    try:
                        process = subprocess.run([sys.executable, '-m', 'scripts.eval_agent', '--worker', str(payload_path),
                                                  '--worker-output', str(output_path)], cwd=ROOT, env=env,
                                                 capture_output=True, text=True, timeout=args.case_timeout)
                        if process.returncode != 0 or not output_path.exists():
                            # Keep exceptions as failures; don't let one broken case stop the suite.
                            row = {'case_id': case.id, 'category': case.category, 'error': 'worker_error',
                                   'latency_ms': round((time.perf_counter() - started) * 1000, 3)}
                            print(f'{case.id}: worker failed (exit {process.returncode})', file=sys.stderr)
                        else:
                            row = json.loads(output_path.read_text(encoding='utf-8'))
                    except subprocess.TimeoutExpired:
                        row = {'case_id': case.id, 'category': case.category, 'error': 'worker_timeout',
                               'latency_ms': round((time.perf_counter() - started) * 1000, 3)}
                    row['repeat'] = repeat
                    rows.append(row)
                    write_jsonl(args.output / 'agent_cases.jsonl', rows)
                    status = 'PASS' if row.get('score', {}).get('passed') else 'FAIL'
                    print(f'{case.id} repeat={repeat} {status}', flush=True)
    report = {'metadata': meta, 'scope': 'Real AgentLoop + default tools + per-case temporary SQLite; one conversation turn, no background memory extraction/compression. Deterministic tool/argument/regex/persistence/provenance checks; no LLM judge. Text regex cannot prove semantic or medical safety.',
              'summary': aggregate_agent(rows), 'by_category': {cat: aggregate_agent([r for r in rows if r['category'] == cat]) for cat in sorted({c.category for c in cases})}, 'cases': rows}
    write_json(args.output / 'agent_report.json', report)
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    return int(any(not row.get('score', {}).get('passed') for row in rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=ROOT / 'eval/agent_cases.jsonl')
    parser.add_argument('--output', type=Path, default=ROOT / 'eval/results/agent')
    parser.add_argument('--case-id', action='append')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--case-timeout', type=float, default=660)
    parser.add_argument('--temperature', type=float, default=0)
    parser.add_argument('--model')
    parser.add_argument('--now', help='ISO timestamp with timezone; fixes prompt, time tool, fixture dates')
    parser.add_argument('--retrieval-mode', choices=['bm25', 'hybrid', 'hybrid_rerank'], default='hybrid_rerank')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--worker-output', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeat < 1 or args.case_timeout <= 0 or (args.limit is not None and args.limit < 1):
        parser.error('repeat, case-timeout and limit must be positive')
    logging.basicConfig(level=logging.WARNING)
    try:
        if args.worker:
            try:
                asyncio.run(worker(args.worker, args.worker_output))
            except Exception as exc:
                write_json(args.worker_output, {'case_id': json.loads(args.worker.read_text())['case']['id'],
                           'category': json.loads(args.worker.read_text())['case']['category'], 'error': type(exc).__name__,
                           'error_message': str(exc) if isinstance(exc, (ValueError, RuntimeError)) else None})
                # Serialized failure is returned to the supervisor and included in its denominator.
            return
        raise SystemExit(run(args))
    except (ValueError, RuntimeError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    main()
