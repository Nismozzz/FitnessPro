"""python -m scripts.eval_retrieval --help; usage and label protocol: eval/PROTOCOL.md."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from eval.common import ROOT, RetrievalCase, counts, load_cases, mean, metadata, percentile, retrieval_metrics, write_json, write_jsonl

MODES = {"bm25": ("bm25", False), "dense": ("dense", False), "hybrid": ("hybrid", False), "hybrid_rerank": ("hybrid", True)}


def validate_catalog(cases, docs):
    from app.rag.schema import ExerciseFilter
    for case in cases:
        # Reject typo filters instead of ExerciseFilter's production extra='ignore'.
        if set(case.filters) - set(ExerciseFilter.model_fields):
            raise ValueError(f"{case.id}: unknown filter keys")
        filters = ExerciseFilter.model_validate(case.filters)
        for eid in case.relevant:
            if eid not in docs:
                raise ValueError(f"{case.id}: unknown relevant id {eid}")
            if not filters.matches(docs[eid]):
                raise ValueError(f"{case.id}: relevant id violates filters: {eid}")


def aggregate(rows, k):
    eligible = [r for r in rows if r['status'] == 'ok']
    return {"cases": len(rows), "scored_cases": len(eligible), "status_counts": counts(r['status'] for r in rows),
            f"recall@{k}": mean(r['metrics']['recall'] for r in eligible),
            f"mrr@{k}": mean(r['metrics']['mrr'] for r in eligible),
            f"ndcg@{k}": mean(r['metrics']['ndcg'] for r in eligible),
            "positive_cases_scored": sum(not r['no_answer'] for r in eligible),
            "no_answer_cases_scored": sum(r['no_answer'] for r in eligible),
            "no_answer_accuracy": mean(r['metrics']['no_answer_correct'] for r in eligible),
            "filter_compliance": mean(r['filter_compliant'] for r in eligible),
            "latency_ms_mean": mean(r['latency_ms'] for r in rows if r.get('latency_ms') is not None),
            "latency_ms_p50": percentile([r['latency_ms'] for r in rows if r.get('latency_ms') is not None], .5),
            "latency_ms_p95": percentile([r['latency_ms'] for r in rows if r.get('latency_ms') is not None], .95)}


async def evaluate(cases, retriever, modes, k, pool_depth=None):
    from app.rag.schema import ExerciseFilter
    rows = []
    for mode in modes:
        route, rerank = MODES[mode]
        unavailable = []
        if route != 'bm25' and (retriever.vector_store is None or retriever.embedder is None):
            unavailable.append('dense index/embedder unavailable')
        elif route != 'bm25' and set(retriever.vector_store.ids) != set(retriever.docs):
            unavailable.append('vector index ids differ from catalog')
        if rerank and retriever.reranker is None:
            unavailable.append('reranker unavailable or disabled in config')
        for case in cases:
            base = {"case_id": case.id, "category": case.category, "requested_mode": mode, "no_answer": case.no_answer,
                    "label_status": case.label_status, "hit_ids": [], "metrics": None, "latency_ms": None}
            if unavailable:
                rows.append({**base, "status": "unavailable", "warnings": unavailable})
                continue
            filters = ExerciseFilter.model_validate(case.filters)
            # Cold query cache for each case/mode; no pre-warm advantage for hybrid.
            retriever._query_cache.clear()
            start = time.perf_counter()
            try:
                result = await retriever.search(case.query, filters, max(k, pool_depth or k), mode=route, rerank=rerank)
                ids = [hit.doc.id for hit in result.hits]
                degradation = any('向量检索暂不可用' in w or '重排服务暂不可用' in w for w in result.warnings)
                rows.append({**base, "status": "degraded" if degradation else "ok", "actual_mode": result.mode,
                             "latency_ms": round((time.perf_counter() - start) * 1000, 3), "warnings": result.warnings,
                             "hit_ids": ids[:k], "pool_hit_ids": ids, "total_candidates": result.total_candidates,
                             "filter_compliant": all(filters.matches(h.doc) for h in result.hits),
                             "metrics": retrieval_metrics(ids, case.relevant, k, case.no_answer)})
            except Exception as exc:
                rows.append({**base, "status": "error", "error_type": type(exc).__name__,
                             "latency_ms": round((time.perf_counter() - start) * 1000, 3)})
        print(f"{mode}: {counts(r['status'] for r in rows if r['requested_mode'] == mode)}", flush=True)
    return rows


def build_pool(cases, rows, docs):
    pool = []
    for case in cases:
        ranked = {r['requested_mode']: r.get('pool_hit_ids', r['hit_ids']) for r in rows if r['case_id'] == case.id}
        ids = sorted(set(case.relevant) | {eid for hits in ranked.values() for eid in hits})
        pool.append({"case_id": case.id, "query": case.query, "filters": case.filters, "rankings": ranked,
                     "pool_modes": sorted(ranked), "pool_complete": set(ranked) == set(MODES) and all(r["status"] == "ok" for r in rows if r["case_id"] == case.id),
                     "no_answer": case.no_answer, "candidates": [
                         {"exercise_id": eid, "document": asdict(docs[eid]), "seed_relevant": eid in case.relevant,
                          "relevance": None} for eid in ids], "reviewed": False})
    return pool


def apply_judgments(cases, path, docs):
    lines = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    ids = [row['case_id'] for row in lines]
    if len(ids) != len(set(ids)) or set(ids) != {c.id for c in cases}:
        raise ValueError('judgments must contain exactly one row for every selected case')
    lookup = {row['case_id']: row for row in lines}
    for case in cases:
        row = lookup[case.id]
        if not row.get('reviewed') or not row.get('pool_complete') or set(row.get('pool_modes', [])) != set(MODES) or row['query'] != case.query or row['filters'] != case.filters:
            raise ValueError(f'{case.id}: unreviewed or mismatched judgments')
        candidates = row['candidates']
        candidate_ids = [c['exercise_id'] for c in candidates]
        if len(candidate_ids) != len(set(candidate_ids)) or not set(case.relevant) <= set(candidate_ids):
            raise ValueError(f'{case.id}: duplicate candidates or omitted seed labels')
        if any(c['exercise_id'] not in docs or type(c['relevance']) is not int or c['relevance'] not in (0, 1) for c in candidates):
            raise ValueError(f'{case.id}: every candidate needs a 0/1 judgment and valid id')
        case.relevant = [c['exercise_id'] for c in candidates if c['relevance'] == 1]
        case.no_answer = row['no_answer']
        case.label_status = 'reviewed'
        RetrievalCase.model_validate(case.model_dump())
    validate_catalog(cases, docs)


async def run(args):
    from app.config import settings
    from app.rag.retriever import ExerciseRetriever
    if args.judgments and not args.judgments.is_file():
        raise ValueError(
            f"找不到复核标签文件：{args.judgments}。四组消融不会自动生成此文件。"
            "若沿用初始标签，请去掉 --judgments；若要使用人工复核标签，"
            "先用 --pool-output eval/results/pool.jsonl 导出候选池，复制为该文件并完成标注。"
            "详见 eval/PROTOCOL.md 的标签复核章节。"
        )
    cases = load_cases(args.cases, RetrievalCase)
    if args.limit:
        cases = cases[:args.limit]
    dense = any(m != 'bm25' for m in args.modes) and not args.validate_only
    retriever = await ExerciseRetriever.load(enable_dense=dense, enable_rerank='hybrid_rerank' in args.modes and not args.validate_only)
    validate_catalog(cases, retriever.docs)
    if args.judgments:
        apply_judgments(cases, args.judgments, retriever.docs)
    if args.validate_only:
        print(f'Validated {len(cases)} retrieval cases against {len(retriever.docs)} catalog exercises; no API calls.')
        return 0
    rows = await evaluate(cases, retriever, args.modes, args.k, args.pool_depth if args.pool_output else None)
    config = {"modes": args.modes, "k": args.k, "retrieval": settings.retrieval.model_dump(),
              "embedding_model": settings.embedding.model, "reranker_model": settings.reranker.model,
              "rerank_candidates": settings.reranker.candidates, "cache_policy": "cold_per_case", "pool_depth": args.pool_depth if args.pool_output else None, "judgments_sha256": None}
    if args.judgments:
        import hashlib
        config['judgments_sha256'] = hashlib.sha256(args.judgments.read_bytes()).hexdigest()
    report = {"metadata": metadata(args.cases, [asdict(d) for d in retriever.docs.values()], config),
              "label_status_counts": counts(c.label_status for c in cases),
              "scoring_note": "Binary seed labels are provisional; review a four-mode pooled candidate union before drawing conclusions. Unjudged hits count as nonrelevant. No-answer cases excluded from ranking averages; scored separately. Degraded/unavailable/error rows excluded, with explicit coverage counts.",
              "summary": {m: aggregate([r for r in rows if r['requested_mode'] == m], args.k) for m in args.modes},
              "by_category": {m: {cat: aggregate([r for r in rows if r['requested_mode'] == m and r['category'] == cat], args.k)
                                   for cat in sorted({c.category for c in cases})} for m in args.modes}, "cases": rows}
    write_json(args.output / 'retrieval_report.json', report)
    write_jsonl(args.output / 'retrieval_cases.jsonl', rows)
    if args.pool_output:
        write_jsonl(args.pool_output, build_pool(cases, rows, retriever.docs))
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    return int(any(r['status'] != 'ok' for r in rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=ROOT / 'eval/retrieval_cases.jsonl')
    parser.add_argument('--modes', nargs='+', choices=MODES, default=list(MODES))
    parser.add_argument('--k', type=int, default=5)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--output', type=Path, default=ROOT / 'eval/results/retrieval')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--pool-output', type=Path, help='Export union of all selected modes, plus seed labels, for 0/1 annotation')
    parser.add_argument('--pool-depth', type=int, default=15, help='Candidate depth per mode for annotation; can alter reranking pool latency, use a separate output directory')
    parser.add_argument('--judgments', type=Path, help='Read a reviewed pool JSONL to replace provisional labels')
    args = parser.parse_args()
    if args.pool_depth < 1 or args.k < 1 or (args.limit is not None and args.limit < 1):
        parser.error('k and limit must be positive')
    logging.basicConfig(level=logging.WARNING)
    try:
        raise SystemExit(asyncio.run(run(args)))
    except (ValueError, RuntimeError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    main()
