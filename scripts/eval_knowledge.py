"""独立文献检索种子集：按文献去重评估 Recall/MRR@k，未做人工相关性复审。"""
import argparse
import asyncio
import json
from pathlib import Path

from app.knowledge.retriever import ArticleRetriever
from app.config import resolve_path


async def evaluate(retriever, cases, k=3, mode='bm25', rerank=False):
    rows = []
    for case in cases:
        result = await retriever.search(case['query'], top_k=len(retriever.docs), mode=mode, rerank=rerank)
        unique = list(dict.fromkeys(hit.doc.source_file for hit in result.hits))[:k]
        prefix = case['source_prefix']
        rank = next((i for i, name in enumerate(unique, 1) if prefix and name.startswith(prefix)), None)
        rows.append({**case, 'sources': unique, 'rank': rank, 'mode': result.mode, 'warnings': result.warnings,
                     'no_answer_correct': not unique if prefix is None else None})
    positives = [r for r in rows if r['source_prefix']]
    negatives = [r for r in rows if not r['source_prefix']]
    return {'label_status': 'seed_not_independently_reviewed', 'cases': len(rows),
            f'recall@{k}': sum(r['rank'] is not None for r in positives) / len(positives),
            f'mrr@{k}': sum(1/r['rank'] if r['rank'] else 0 for r in positives) / len(positives),
            'no_answer_accuracy': sum(r['no_answer_correct'] for r in negatives)/len(negatives), 'rows': rows}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['bm25', 'hybrid', 'hybrid-rerank'], default='bm25')
    parser.add_argument('--output', type=Path, default=resolve_path('eval/results/knowledge/report.json'))
    args = parser.parse_args()
    dense, rerank = args.mode != 'bm25', args.mode == 'hybrid-rerank'
    retriever = ArticleRetriever.load(enable_dense=dense, enable_rerank=rerank)
    if dense and retriever.vector_store is None:
        raise RuntimeError('向量索引不可用，不能将降级结果报告为混合检索评测')
    if rerank and retriever.reranker is None:
        raise RuntimeError('重排器不可用')
    cases = json.loads(resolve_path('eval/knowledge/cases.json').read_text(encoding='utf-8'))
    report = await evaluate(retriever, cases, mode='hybrid' if dense else 'bm25', rerank=rerank)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k != 'rows'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
