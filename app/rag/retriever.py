"""
动作检索服务。

流程：结构化过滤 → 稠密向量检索 + BM25（都限制在候选集合内）→ RRF 融合 → 可选重排。

- 过滤条件由 Agent 的工具参数直接给出（枚举值），不再额外调用一次 LLM 抽取过滤条件；
- 一条动作就是一个文档（中位数约 100 个英文单词），无需切块，也就没有“小块召回、
  父文档回填”的上下文膨胀问题；
- 向量检索或重排失败时降级而不是报错，并在结果中给出 warnings。
"""

from __future__ import annotations

import logging
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field

import numpy as np

from app.config import RetrievalSettings, resolve_path, settings
from app.rag.bm25 import BM25Index
from app.rag.embeddings import Embedder, create_embedder
from app.rag.reranker import Reranker
from app.rag.schema import LEVEL_ORDER, ExerciseDoc, ExerciseFilter
from app.rag.vector_store import LocalVectorStore

logger = logging.getLogger(__name__)


class KnowledgeBaseNotReady(RuntimeError):
    pass


def rrf_fuse(rankings: list[list[tuple[str, float]]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion：只看名次，不受两路分数尺度不同的影响。"""
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, (doc_id, _score) in enumerate(ranking, start=1):
            scores[doc_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


def default_order_key(doc: ExerciseDoc) -> tuple:
    """无查询词时的默认排序：复合动作优先，难度由低到高。"""
    return (doc.mechanic != "compound", LEVEL_ORDER.get(doc.level, 9), doc.name)


@dataclass
class SearchHit:
    doc: ExerciseDoc
    score: float
    dense_rank: int | None = None
    sparse_rank: int | None = None
    rerank_score: float | None = None


@dataclass
class SearchResult:
    hits: list[SearchHit]
    total_candidates: int
    mode: str
    warnings: list[str] = field(default_factory=list)


class ExerciseRetriever:
    def __init__(
        self,
        docs: list[ExerciseDoc],
        cfg: RetrievalSettings,
        vector_store: LocalVectorStore | None = None,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        rerank_candidates: int = 20,
    ):
        self.cfg = cfg
        self.docs = {d.id: d for d in docs}
        self._order = [d.id for d in docs]
        self.bm25 = BM25Index(self._order, [d.search_text() for d in docs])
        self.vector_store = vector_store if embedder is not None else None
        self.embedder = embedder
        self.reranker = reranker
        self.rerank_candidates = rerank_candidates
        self._query_cache: OrderedDict[str, np.ndarray] = OrderedDict()

    # ---------------- 加载 ----------------

    @classmethod
    async def load(cls, *, enable_dense: bool = True, enable_rerank: bool = True) -> "ExerciseRetriever":
        from sqlalchemy import select

        from app.database.models import Exercise
        from app.database.session import SessionLocal

        async with SessionLocal() as db:
            rows = (await db.execute(select(Exercise))).scalars().all()
        if not rows:
            raise KnowledgeBaseNotReady(
                "动作知识库为空，请先运行：python -m scripts.ingest_exercises"
            )
        docs = [ExerciseDoc.from_model(r) for r in rows]

        store = LocalVectorStore.load(resolve_path(settings.data.index_dir)) if enable_dense else None
        embedder: Embedder | None = None
        if store is None and enable_dense:
            logger.warning("未找到向量索引，仅使用 BM25 检索")
        elif store is not None and store.meta.get("embedding_model") != settings.embedding.model:
            logger.warning(
                "向量索引的模型 %s 与当前配置 %s 不一致，已禁用向量检索，请重新入库",
                store.meta.get("embedding_model"),
                settings.embedding.model,
            )
            store = None
        elif store is not None:
            try:
                embedder = create_embedder(settings.embedding)
            except Exception as exc:  # 缺少 key 等
                logger.warning("Embedding 初始化失败，仅使用 BM25：%s", exc)
                store = None

        reranker = None
        if enable_rerank and settings.reranker.enabled:
            try:
                reranker = Reranker(settings.reranker)
            except Exception as exc:
                logger.warning("Reranker 初始化失败，跳过重排：%s", exc)

        retriever = cls(
            docs,
            settings.retrieval,
            vector_store=store,
            embedder=embedder,
            reranker=reranker,
            rerank_candidates=settings.reranker.candidates,
        )
        logger.info(
            "知识库已加载：%d 个动作，向量索引=%s，重排=%s",
            len(docs),
            "on" if retriever.vector_store else "off",
            "on" if reranker else "off",
        )
        return retriever

    # ---------------- 查询 ----------------

    def get(self, doc_id: str) -> ExerciseDoc | None:
        return self.docs.get(doc_id)

    async def _embed_query(self, query: str) -> np.ndarray:
        cached = self._query_cache.get(query)
        if cached is not None:
            self._query_cache.move_to_end(query)
            return cached
        assert self.embedder is not None
        vec = await self.embedder.embed_query(query)
        self._query_cache[query] = vec
        if len(self._query_cache) > 512:
            self._query_cache.popitem(last=False)
        return vec

    async def search(
        self, query: str, filters: ExerciseFilter | None = None, top_k: int | None = None,
        *, mode: str = "hybrid", rerank: bool | None = None,
    ) -> SearchResult:
        if mode not in {"bm25", "dense", "hybrid"}:
            raise ValueError("mode must be bm25, dense, or hybrid")
        filters = filters or ExerciseFilter()
        top_k = top_k or self.cfg.default_top_k
        candidates = [self.docs[i] for i in self._order if filters.matches(self.docs[i])]
        if not candidates:
            return SearchResult(
                hits=[],
                total_candidates=0,
                mode="filter_only",
                warnings=["没有满足筛选条件的动作，可以放宽器械、难度或肌群条件后重试"],
            )

        query = query.strip()
        if not query:
            ordered = sorted(candidates, key=default_order_key)[:top_k]
            return SearchResult(
                hits=[SearchHit(d, 0.0) for d in ordered],
                total_candidates=len(candidates),
                mode="filter_only",
            )

        allowed = {d.id for d in candidates}
        warnings: list[str] = []

        dense: list[tuple[str, float]] = []
        if mode != "bm25" and self.vector_store is not None:
            try:
                qvec = await self._embed_query(query)
                dense = self.vector_store.search(qvec, self.cfg.dense_top_k, allowed)
            except Exception as exc:
                logger.warning("向量检索失败，降级为关键词检索：%s", exc)
                warnings.append("向量检索暂不可用，本次结果仅基于关键词匹配")
        sparse = self.bm25.search(query, self.cfg.sparse_top_k, allowed) if mode != "dense" else []

        fused = rrf_fuse([r for r in (dense, sparse) if r], k=self.cfg.rrf_k)
        if not fused:
            ordered = sorted(candidates, key=default_order_key)[:top_k]
            return SearchResult(
                hits=[SearchHit(d, 0.0) for d in ordered],
                total_candidates=len(candidates),
                mode="filter_only",
                warnings=warnings + ["没有与查询词直接相关的动作，以下是满足筛选条件的常用动作"],
            )

        dense_rank = {doc_id: i for i, (doc_id, _) in enumerate(dense, start=1)}
        sparse_rank = {doc_id: i for i, (doc_id, _) in enumerate(sparse, start=1)}
        hits = [
            SearchHit(
                self.docs[doc_id],
                score,
                dense_rank=dense_rank.get(doc_id),
                sparse_rank=sparse_rank.get(doc_id),
            )
            for doc_id, score in fused
        ]
        mode = "hybrid" if dense and sparse else "dense_only" if dense else "sparse_only"

        if self.reranker is not None and rerank is not False and len(hits) > 1:
            pool = hits[: self.rerank_candidates]
            try:
                ranked = await self.reranker.rerank(
                    query, [h.doc.search_text() for h in pool], top_n=top_k
                )
                reranked = []
                for idx, score in ranked:
                    hit = pool[idx]
                    hit.rerank_score = score
                    reranked.append(hit)
                hits = reranked
                mode += "+rerank"
            except Exception as exc:
                logger.warning("重排失败，使用融合排序：%s", exc)
                warnings.append("重排服务暂不可用，使用融合排序结果")

        return SearchResult(
            hits=hits[:top_k],
            total_candidates=len(candidates),
            mode=mode,
            warnings=warnings,
        )


_retriever: ExerciseRetriever | None = None
_load_error: str | None = None


async def init_retriever() -> None:
    global _retriever, _load_error
    try:
        _retriever = await ExerciseRetriever.load()
        _load_error = None
    except Exception as exc:
        _retriever = None
        _load_error = str(exc)
        logger.warning("知识库未就绪：%s", exc)


def get_retriever() -> ExerciseRetriever:
    if _retriever is None:
        raise KnowledgeBaseNotReady(
            _load_error or "知识库未初始化，请先运行：python -m scripts.ingest_exercises"
        )
    return _retriever


def set_retriever(retriever: ExerciseRetriever | None) -> None:
    """测试或热更新时替换实例。"""
    global _retriever
    _retriever = retriever
