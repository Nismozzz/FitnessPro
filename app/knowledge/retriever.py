from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from app.config import resolve_path, settings
from app.knowledge.ingest import ArticleChunk
from app.rag.bm25 import BM25Index
from app.rag.embeddings import create_embedder
from app.rag.reranker import Reranker
from app.rag.retriever import KnowledgeBaseNotReady, SearchHit, SearchResult, rrf_fuse
from app.rag.storage import load_store

logger = logging.getLogger(__name__)


def catalog_hash(chunks: list[ArticleChunk]) -> str:
    return hashlib.sha256(json.dumps([c.model_dump() for c in chunks], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ArticleRetriever:
    def __init__(self, chunks: list[ArticleChunk], store=None, embedder=None, reranker=None, warnings=None):
        self.docs = {c.id: c for c in chunks}
        self.bm25 = BM25Index([c.id for c in chunks], [c.search_text() for c in chunks])
        self.vector_store = store
        self.embedder = embedder
        self.reranker = reranker
        self.warnings = warnings or []

    @classmethod
    def load(cls, directory: Path | None = None, *, enable_dense=True, enable_rerank=True):
        directory = directory or resolve_path(settings.knowledge.index_dir)
        path = directory / 'chunks.json'
        if not path.exists():
            raise KnowledgeBaseNotReady('文献库未入库，请运行 python -m scripts.ingest_knowledge')
        chunks = [ArticleChunk.model_validate(c) for c in json.loads(path.read_text(encoding='utf-8'))]
        if not chunks or len({c.id for c in chunks}) != len(chunks):
            raise KnowledgeBaseNotReady('文献库为空或有重复 id，请重新入库')
        warnings = []
        store, embedder, reranker = None, None, None
        if enable_dense:
            try:
                store = load_store(directory)
                if store is None:
                    raise ValueError('缺少向量索引')
                if store.meta.get('embedding_model') != settings.embedding.model or store.meta.get('catalog_hash') != catalog_hash(chunks) or store.ids != [c.id for c in chunks]:
                    raise ValueError('向量索引与文献或模型不一致')
                embedder = create_embedder(settings.embedding)
            except Exception as exc:
                logger.warning('文献向量索引不可用: %s', type(exc).__name__)
                warnings.append('文献向量索引不可用，本次仅关键词检索；请重新入库或检查配置')
                store = None
        if enable_rerank and settings.reranker.enabled:
            try:
                reranker = Reranker(settings.reranker)
            except Exception:
                warnings.append('文献重排服务暂不可用')
        return cls(chunks, store, embedder, reranker, warnings)

    async def search(self, query: str, top_k: int = 3, *, mode='hybrid', rerank=True) -> SearchResult:
        from app.database.models import RetrievalLog
        from app.observability.recorder import record
        start, result, error = time.perf_counter(), None, None
        try:
            result = await self._search(query, top_k, mode=mode, rerank=rerank)
            return result
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            record(RetrievalLog, query=query, filters={'corpus': 'articles'},
                   mode=result.mode if result else mode, candidates=len(self.docs),
                   hit_ids=[h.doc.id for h in result.hits] if result else [],
                   latency_ms=(time.perf_counter() - start) * 1000,
                   degraded=bool(result and result.warnings), status='error' if error else 'success', error=error)

    async def _search(self, query, top_k, *, mode, rerank):
        if mode not in {'bm25', 'dense', 'hybrid'}:
            raise ValueError('无效检索模式')
        cfg = settings.retrieval
        warnings = list(self.warnings) if mode != 'bm25' else []
        dense, sparse = [], []
        if mode != 'bm25' and self.vector_store is not None and self.embedder is not None:
            try:
                dense = await asyncio.to_thread(self.vector_store.search, await self.embedder.embed_query(query), cfg.dense_top_k)
            except Exception:
                warnings.append('文献向量检索暂不可用，降级关键词检索')
        if mode != 'dense':
            sparse = self.bm25.search(query, cfg.sparse_top_k)
        fused = rrf_fuse([r for r in (dense, sparse) if r], cfg.rrf_k)
        hits = [SearchHit(self.docs[doc_id], score) for doc_id, score in fused]
        actual = 'hybrid' if dense and sparse else 'dense_only' if dense else 'sparse_only'
        if hits and self.reranker is not None and rerank:
            pool = hits[:settings.reranker.candidates]
            try:
                ranked = await self.reranker.rerank(query, [h.doc.search_text() for h in pool], top_n=top_k)
                hits = [SearchHit(pool[idx].doc, pool[idx].score, rerank_score=score) for idx, score in ranked]
                actual += '+rerank'
            except Exception:
                warnings.append('文献重排服务暂不可用，使用融合排序')
        # 与动作库不同：无词汇/向量命中时绝不回填无关文献。
        return SearchResult(hits[:top_k], len(self.docs), actual, warnings)


_retriever: ArticleRetriever | None = None


def init_article_retriever():
    global _retriever
    try:
        _retriever = ArticleRetriever.load()
    except Exception as exc:
        _retriever = None
        logger.warning('文献库未就绪: %s', exc)


def get_article_retriever() -> ArticleRetriever:
    if _retriever is None:
        raise KnowledgeBaseNotReady('文献库未就绪，请运行 python -m scripts.ingest_knowledge 并重启服务')
    return _retriever
