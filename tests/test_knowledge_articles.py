import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from app.agent.citations import EvidenceRegistry, history_citations
from app.agent.tools.article_tools import SearchKnowledgeArticlesTool
from app.agent.tools.base import ToolContext, ToolExecutor
from app.agent.tools.registry import build_default_tools
from app.agent.types import ToolCall
from app.config import settings
from app.evaluation.service import extract_contexts
from app.knowledge.ingest import ArticleChunk, read_articles, recursive_split
from app.knowledge.retriever import ArticleRetriever, catalog_hash
from app.rag.retriever import KnowledgeBaseNotReady
from app.rag.vector_store import LocalVectorStore
from app.utils.background import drain
from scripts.ingest_knowledge import ingest


@pytest.fixture(autouse=True)
async def cleanup_telemetry(db):
    yield
    await drain()


@pytest.fixture
def corpus(tmp_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    for i, (title, body) in enumerate([
        ('容量管理荟萃分析', '每周肌群组数容量与肌肥大存在剂量效应关系。敏感性分析提示不稳健。'),
        ('训练频率综述', '每肌群频率与训练课次频率不同，容量对等时应保留研究条件。'),
        ('低负荷高负荷综述', '高负荷最大力量1RM研究，低负荷力竭肌肥大没有显著差异。'),
    ]):
        (raw / f'{i}.md').write_text(f'---\ntitle: {title}\nauthors: Author\nsource_url: https://doi.org/10.1/test{i}\n---\n## 结论\n{body}', encoding='utf-8')
    return raw


def test_recursive_split_keeps_content_and_bounds():
    text = ('第一段说明条件。' * 30) + '\n\n' + ('第二段局限性；' * 50)
    chunks = recursive_split(text, 120)
    assert all(len(c) <= 120 for c in chunks)
    assert ''.join(chunks) == text
    assert recursive_split('纯字符' * 100, 100)
    with pytest.raises(ValueError):
        recursive_split('x', 1)


def test_source_metadata_validation_and_stable_ids(corpus):
    first = read_articles(corpus, 100)
    assert first == read_articles(corpus, 100)
    assert len({c.id for c in first}) == len(first)
    assert all(c.title and c.authors and c.source_url and c.section == '结论' for c in first)
    path = corpus / '0.md'
    path.write_text(path.read_text().replace('source_url: https://doi.org/10.1/test0', 'source_url: javascript:alert(1)'))
    with pytest.raises(ValueError, match='HTTPS'):
        read_articles(corpus)


async def test_ingestion_is_repeatable_skip_embed_removes_old_vectors_and_failed_embedding_preserves_index(corpus, tmp_path, monkeypatch):
    directory = tmp_path / 'index'
    fake = SimpleNamespace(embed_documents=AsyncMock(return_value=np.eye(3, dtype=np.float32)))
    monkeypatch.setattr('scripts.ingest_knowledge.create_embedder', lambda cfg: fake)
    manifest = await ingest(corpus, directory)
    assert manifest['articles'] == 3 and manifest['dense']
    old = (directory / 'chunks.json').read_bytes()
    fake.embed_documents.side_effect = RuntimeError('network')
    with pytest.raises(RuntimeError):
        await ingest(corpus, directory)
    assert old == (directory / 'chunks.json').read_bytes()
    assert (directory / 'vectors.npy').exists()
    await ingest(corpus, directory, skip_embed=True)
    assert not (directory / 'vectors.npy').exists()
    assert ArticleRetriever.load(directory, enable_dense=False, enable_rerank=False).docs
    with pytest.raises(ValueError, match='覆盖原始'):
        await ingest(corpus, corpus, skip_embed=True)


async def test_hybrid_reuses_rrf_rerank_and_degrades(corpus):
    chunks = read_articles(corpus)
    store = LocalVectorStore([c.id for c in chunks], np.eye(3))
    embedder = SimpleNamespace(embed_query=AsyncMock(return_value=np.array([1, 0, 0])))
    reranker = SimpleNamespace(rerank=AsyncMock(return_value=[(0, .9)]))
    retriever = ArticleRetriever(chunks, store, embedder, reranker)
    result = await retriever.search('周容量组数', 1)
    assert result.mode == 'hybrid+rerank'
    assert result.hits[0].doc.id == chunks[0].id
    embedder.embed_query.side_effect = RuntimeError()
    reranker.rerank.side_effect = RuntimeError()
    result = await retriever.search('容量组数')
    assert result.hits and len(result.warnings) == 2
    result = await retriever.search('天文望远镜', mode='bm25', rerank=False)
    assert not result.hits  # 不回填无关资料


async def test_mismatched_catalog_disables_dense(corpus, tmp_path):
    directory = tmp_path / 'index'
    await ingest(corpus, directory, skip_embed=True)
    chunks = read_articles(corpus)
    LocalVectorStore([c.id for c in chunks], np.eye(3), {'embedding_model': settings.embedding.model, 'catalog_hash': 'stale'}).save(directory)
    loaded = ArticleRetriever.load(directory, enable_rerank=False)
    assert loaded.vector_store is None and loaded.warnings
    assert catalog_hash(chunks) != 'stale'


async def test_tool_citations_history_and_ragas_use_only_visible_complete_evidence(corpus, monkeypatch):
    retriever = ArticleRetriever(read_articles(corpus))
    monkeypatch.setattr('app.agent.tools.article_tools.get_article_retriever', lambda: retriever)
    result = await ToolExecutor([SearchKnowledgeArticlesTool()]).execute(
        ToolCall('a', 'search_knowledge_articles', {'query': '容量组数'}), ToolContext('u', 's'))
    assert result.success and result.data['articles']
    registry = EvidenceRegistry()
    content = registry.tool_content(result.data, 2500)
    evidence = json.loads(content)['data']['evidence'][0]
    assert evidence['source_type'] == 'article' and evidence['authors']
    assert evidence['text'] == result.data['articles'][0]['text']
    answer = f"资料中存在剂量效应关系[{evidence['id']}]"
    assert registry.resolve(answer)[1] == [evidence]
    rows = [SimpleNamespace(turn_id='t', role='tool', name='search_knowledge_articles', content=content),
            SimpleNamespace(turn_id='t', role='assistant', tool_calls=None, content=answer, seq=2)]
    assert history_citations(rows)[2] == [evidence]
    transcript = [{'role': 'tool', 'name': 'search_knowledge_articles', 'content': content}]
    assert extract_contexts(transcript, 12000) == [content]
    assert not extract_contexts([{'role':'tool', 'name':'search_knowledge_articles', 'content':'{"ok":false,"error":"x"}'}], 12000)
    tiny = json.loads(EvidenceRegistry().tool_content(result.data, 10))
    assert not tiny['data']['evidence']
    assert 'search_knowledge_articles' in {t.name for t in build_default_tools()}


async def test_tool_not_ready_and_invalid_parameters(monkeypatch):
    def missing():
        raise KnowledgeBaseNotReady('文献库未就绪')
    monkeypatch.setattr('app.agent.tools.article_tools.get_article_retriever', missing)
    executor = ToolExecutor([SearchKnowledgeArticlesTool()])
    for arguments in [{'query': ' '}, {'query': '组数', 'top_k': 7}, {'query': '组数', 'user_id': 'other'}]:
        result = await executor.execute(ToolCall('a', 'search_knowledge_articles', arguments), ToolContext('u', 's'))
        assert not result.success
    result = await executor.execute(ToolCall('a', 'search_knowledge_articles', {'query': '容量'}), ToolContext('u', 's'))
    assert not result.success and '未就绪' in result.error


async def test_literature_seed_eval(corpus):
    from scripts.eval_knowledge import evaluate
    retriever = ArticleRetriever(read_articles(corpus))
    report = await evaluate(retriever, [{'query':'周容量组数', 'source_prefix':'0'}, {'query':'天文望远镜', 'source_prefix':None}])
    assert report['recall@3'] == 1 and report['no_answer_accuracy'] == 1


async def test_agent_loop_emits_literature_citations_and_saves_evidence_snapshot(corpus, monkeypatch):
    from app.agent.loop import AgentLoop
    from app.config import AgentSettings
    from tests.test_agent import chunk, tc
    monkeypatch.setattr('app.agent.tools.article_tools.get_article_retriever', lambda: ArticleRetriever(read_articles(corpus)))

    class Model:
        async def stream(self, messages, **kwargs):
            if messages[-1]['role'] != 'tool':
                yield chunk(tool_calls=[tc(0, 'article_call', 'search_knowledge_articles', '{"query":"容量组数"}')])
            else:
                evidence = json.loads(messages[-1]['content'])['data']['evidence'][0]
                yield chunk(f"摘录指出剂量效应关系，但敏感性分析提示不稳健[{evidence['id']}]。")

    transcript = []
    loop = AgentLoop(Model(), ToolExecutor([SearchKnowledgeArticlesTool()]), AgentSettings(max_iterations=4))
    events = [e async for e in loop.run([{'role':'user', 'content':'容量组数的依据？'}], ToolContext('u', 's'), transcript)]
    citations = next(e for e in events if e.type == 'citations').data['citations']
    assert citations[0]['source_type'] == 'article'
    assert events[-1].type == 'done' and events[-1].data['status'] == 'completed'
    saved_context = extract_contexts(transcript, 12000)
    assert citations[0] in json.loads(saved_context[0])['data']['evidence']
