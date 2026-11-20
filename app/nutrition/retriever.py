from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from app.config import resolve_path, settings
from app.nutrition.ingest import MealChunk, catalog_hash
from app.rag.bm25 import BM25Index
from app.rag.embeddings import create_embedder
from app.rag.retriever import SearchHit, SearchResult, rrf_fuse
from app.rag.storage import load_store

logger = logging.getLogger(__name__)


class NutritionKnowledgeNotReady(RuntimeError):
    pass


class MealRetriever:
    def __init__(self, meals: list[MealChunk], store=None, embedder=None):
        self.docs = {m.id: m for m in meals}
        self.bm25 = BM25Index([m.id for m in meals], [m.search_text() for m in meals])
        self.vector_store, self.embedder = store, embedder
        self.warnings = [] if store is not None else ["菜谱向量索引不可用，本次仅关键词检索"]

    @classmethod
    def load(cls, directory: Path | None = None):
        directory = directory or resolve_path(settings.nutrition.index_dir)
        path = directory / "meals.json"
        if not path.exists():
            raise NutritionKnowledgeNotReady("菜谱库未入库，请运行 python -m scripts.ingest_meals")
        meals = [MealChunk.model_validate(x) for x in json.loads(path.read_text(encoding="utf-8"))]
        store = None
        embedder = None
        try:
            store = load_store(directory)
            if store is None or store.meta.get("embedding_model") != settings.embedding.model or store.meta.get("catalog_hash") != catalog_hash(meals) or store.ids != [m.id for m in meals]:
                raise ValueError("菜谱向量索引与数据或 embedding 模型不一致")
            embedder = create_embedder(settings.embedding)
        except Exception as exc:
            logger.warning("菜谱向量索引不可用: %s", type(exc).__name__)
            store = None
        return cls(meals, store, embedder)

    async def search(self, query: str, top_k: int = 5, *, category: str | None = None, meal_type: str | None = None) -> SearchResult:
        allowed = {m.id for m in self.docs.values() if (not category or m.category == category) and (not meal_type or meal_type in m.mealType)}
        dense, sparse = [], []
        if self.vector_store is not None and self.embedder is not None and allowed:
            try:
                dense = await asyncio.to_thread(self.vector_store.search, await self.embedder.embed_query(query), settings.retrieval.dense_top_k, allowed)
            except Exception:
                self.warnings.append("菜谱向量检索失败，已降级关键词检索")
        sparse = self.bm25.search(query, settings.retrieval.sparse_top_k, allowed=allowed)
        fused = rrf_fuse([x for x in (dense, sparse) if x], settings.retrieval.rrf_k)
        hits = [SearchHit(self.docs[i], score) for i, score in fused[:top_k]]
        mode = "hybrid" if dense and sparse else "dense_only" if dense else "sparse_only"
        return SearchResult(hits, len(self.docs), mode, list(dict.fromkeys(self.warnings)))


_retriever: MealRetriever | None = None


def init_meal_retriever() -> None:
    global _retriever
    try:
        _retriever = MealRetriever.load()
    except Exception as exc:
        _retriever = None
        logger.warning("菜谱库未就绪: %s", exc)


def get_meal_retriever() -> MealRetriever:
    if _retriever is None:
        raise NutritionKnowledgeNotReady("菜谱库未就绪，请运行 python -m scripts.ingest_meals 并重启服务")
    return _retriever
