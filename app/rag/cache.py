"""Shared exact and semantic caches for public-corpus retrieval results."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import httpx
import numpy as np
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings

if TYPE_CHECKING:
    from app.rag.retriever import SearchResult

logger = logging.getLogger(__name__)
_PAYLOAD_LIMIT = 32 * 1024
EmbedQuery = Callable[[str], Awaitable[np.ndarray]]


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def store_version(store: Any) -> str | None:
    if store is None:
        return None
    if hasattr(store, "collection"):
        return store.collection
    return fingerprint([store.ids, store.meta, hashlib.sha256(store.vectors.tobytes()).hexdigest()])


def retrieval_scope(corpus, catalog, index, cfg, options, *, embedder, reranker=None) -> str:
    embedding_cfg = getattr(embedder, "cfg", settings.embedding) if embedder is not None else None
    rerank_cfg = getattr(reranker, "cfg", settings.reranker) if reranker is not None else None
    return fingerprint([
        corpus, catalog, index, cfg.model_dump(), options,
        embedding_cfg.model_dump() if embedding_cfg is not None else None,
        rerank_cfg.model_dump() if rerank_cfg is not None else None,
    ])


def _restore(raw: str | bytes, docs: Mapping, tier: str) -> SearchResult | None:
    from app.rag.retriever import SearchHit, SearchResult

    if len(raw) > _PAYLOAD_LIMIT:
        return None
    try:
        data = json.loads(raw)
        rows = data["hits"]
        if not rows or not isinstance(rows, list) or not isinstance(data["mode"], str) or type(data["candidates"]) is not int:
            return None
        hits = []
        for doc_id, score, dense_rank, sparse_rank, rerank_score in rows:
            if (type(score) not in (int, float) or
                    any(rank is not None and type(rank) is not int for rank in (dense_rank, sparse_rank)) or
                    (rerank_score is not None and type(rerank_score) not in (int, float))):
                return None
            hits.append(SearchHit(docs[doc_id], score, dense_rank, sparse_rank, rerank_score))
        return SearchResult(hits, data["candidates"], data["mode"], cache_hit=tier)
    except (ValueError, KeyError, IndexError, TypeError):
        return None


class RetrievalCache:
    def __init__(self, redis: Redis | None = None, qdrant: httpx.AsyncClient | None = None) -> None:
        self.redis = redis
        self.qdrant = qdrant
        self._collections: set[str] = set()
        self._cleanup_after: dict[str, float] = {}

    @property
    def active(self) -> bool:
        return settings.retrieval_cache.enabled and (self.redis is not None or self.qdrant is not None)

    async def search(
        self, scope: str, query: str, docs: Mapping,
        retrieve: Callable[[EmbedQuery | None], Awaitable[SearchResult]],
        *, embed: EmbedQuery | None = None,
    ) -> SearchResult:
        cfg = settings.retrieval_cache
        semantic = self.qdrant is not None and cfg.l2_enabled and embed is not None and bool(query.strip())
        policy = cfg.l2_similarity_threshold if semantic else None
        key = f"fitnesspro:rag:l1:v1:{scope}:{fingerprint([query, policy])}"
        raw = await self._get_exact(key)
        if raw is not None and (result := _restore(raw, docs, "l1")) is not None:
            return result

        vector = None
        embedding_error = None
        usable_vector = None
        if semantic:
            try:
                vector = await embed(query)
            except Exception as exc:
                embedding_error = exc
            if vector is not None and vector.ndim == 1 and np.isfinite(vector).all() and np.any(vector):
                usable_vector = vector
                cached = await self._get_semantic(scope, usable_vector)
                if cached is not None:
                    raw, remaining = cached
                    result = _restore(raw, docs, "l2")
                    if result is not None:
                        await self._set_exact(key, raw, min(cfg.l1_ttl_seconds, remaining))
                        return result

        async def reuse_embedding(_: str) -> np.ndarray:
            if embedding_error is not None:
                raise embedding_error
            assert vector is not None
            return vector

        result = await retrieve(reuse_embedding if semantic else None)
        if result.hits and not result.warnings:
            raw = json.dumps({
                "hits": [[h.doc.id, h.score, h.dense_rank, h.sparse_rank, h.rerank_score] for h in result.hits],
                "mode": result.mode,
                "candidates": result.total_candidates,
            }, ensure_ascii=False, separators=(",", ":")).encode()
            if len(raw) <= _PAYLOAD_LIMIT:
                await self._set_exact(key, raw, cfg.l1_ttl_seconds)
                if usable_vector is not None and result.mode.startswith(("dense", "hybrid")):
                    await self._set_semantic(scope, query, usable_vector, raw.decode())
        return result

    async def _get_exact(self, key: str) -> bytes | None:
        if self.redis is not None:
            try:
                async with asyncio.timeout(settings.retrieval_cache.timeout_seconds):
                    return await self.redis.get(key)
            except (RedisError, TimeoutError) as exc:
                logger.debug("Retrieval cache Redis read failed: %s", type(exc).__name__)
        return None

    async def _set_exact(self, key: str, raw: str | bytes, ttl: int) -> None:
        if self.redis is not None and ttl > 0:
            try:
                async with asyncio.timeout(settings.retrieval_cache.timeout_seconds):
                    await self.redis.set(key, raw, ex=ttl)
            except (RedisError, TimeoutError) as exc:
                logger.debug("Retrieval cache Redis write failed: %s", type(exc).__name__)

    def _collection(self, size: int) -> str:
        return f"/collections/{settings.vector_store.collection_prefix}_retrieval_cache_v1_{size}"

    async def _get_semantic(self, scope: str, vector: np.ndarray) -> tuple[str, int] | None:
        collection = self._collection(len(vector))
        body = {
            "query": vector.tolist(), "limit": 1, "with_payload": True,
            "score_threshold": settings.retrieval_cache.l2_similarity_threshold,
            "filter": {"must": [
                {"key": "scope", "match": {"value": scope}},
                {"key": "expires_at", "range": {"gt": int(time.time())}},
            ]},
        }
        try:
            response = await self.qdrant.post(f"{collection}/points/query", json=body)
            if response.status_code == 404:
                self._collections.discard(collection)
                return None
            response.raise_for_status()
            self._collections.add(collection)
            points = response.json()["result"]["points"]
            if points and points[0]["score"] >= body["score_threshold"]:
                payload = points[0]["payload"]
                remaining = int(payload["expires_at"] - time.time())
                if remaining > 0 and payload["scope"] == scope and isinstance(payload["result"], str):
                    return payload["result"], remaining
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.debug("Retrieval cache Qdrant query failed: %s", type(exc).__name__)
        return None

    async def _set_semantic(self, scope: str, query: str, vector: np.ndarray, raw: str) -> None:
        collection = self._collection(len(vector))
        now = int(time.time())
        point = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}/{fingerprint(query)}")),
            "vector": vector.tolist(),
            "payload": {"scope": scope, "result": raw,
                        "expires_at": now + settings.retrieval_cache.l2_ttl_seconds},
        }
        try:
            if collection not in self._collections:
                response = await self.qdrant.put(collection, json={
                    "vectors": {"size": len(vector), "distance": "Cosine"},
                })
                if response.status_code in {400, 409}:
                    # Another worker may have created the same collection.
                    response = await self.qdrant.get(collection)
                response.raise_for_status()
                self._collections.add(collection)
            response = await self.qdrant.put(f"{collection}/points", params={"wait": "true"},
                                             json={"points": [point]})
            response.raise_for_status()
            if time.monotonic() >= self._cleanup_after.get(collection, 0):
                self._cleanup_after[collection] = time.monotonic() + 60
                response = await self.qdrant.post(f"{collection}/points/delete", params={"wait": "false"}, json={
                    "filter": {"must": [{"key": "expires_at", "range": {"lte": now}}]},
                })
                response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("Retrieval cache Qdrant write failed: %s", type(exc).__name__)


cache = RetrievalCache()
