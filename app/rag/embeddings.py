"""Embedding：默认走 OpenAI 兼容 API（如 SiliconFlow bge-m3），可切换为本地 sentence-transformers。"""

from __future__ import annotations

import asyncio
from typing import Protocol

import numpy as np
from openai import AsyncOpenAI

from app.config import EmbeddingSettings
from app.rag.vector_store import l2_normalize


class Embedder(Protocol):
    model_name: str

    async def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    async def embed_query(self, text: str) -> np.ndarray: ...


class OpenAICompatibleEmbedder:
    def __init__(self, cfg: EmbeddingSettings):
        if not cfg.api_key:
            raise RuntimeError(f"Embedding 未配置 API Key（环境变量 {cfg.api_key_env}）")
        self.cfg = cfg
        self.model_name = cfg.model
        self._client = AsyncOpenAI(
            api_key=cfg.api_key, base_url=cfg.base_url, timeout=cfg.timeout, max_retries=2
        )

    async def _embed(self, texts: list[str]) -> np.ndarray:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.cfg.batch_size):
            batch = texts[start : start + self.cfg.batch_size]
            resp = await self._client.embeddings.create(model=self.cfg.model, input=batch)
            vectors.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        return l2_normalize(np.asarray(vectors, dtype=np.float32))

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        return await self._embed(texts)

    async def embed_query(self, text: str) -> np.ndarray:
        return (await self._embed([self.cfg.query_instruction + text]))[0]


class LocalEmbedder:
    """本地推理（需要额外安装 sentence-transformers）。"""

    def __init__(self, cfg: EmbeddingSettings):
        from sentence_transformers import SentenceTransformer

        self.cfg = cfg
        self.model_name = cfg.model
        self._model = SentenceTransformer(cfg.model, device="cpu")

    async def _embed(self, texts: list[str]) -> np.ndarray:
        vectors = await asyncio.to_thread(
            self._model.encode,
            texts,
            batch_size=self.cfg.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        return await self._embed(texts)

    async def embed_query(self, text: str) -> np.ndarray:
        return (await self._embed([self.cfg.query_instruction + text]))[0]


def create_embedder(cfg: EmbeddingSettings) -> Embedder:
    if cfg.provider == "local":
        return LocalEmbedder(cfg)
    return OpenAICompatibleEmbedder(cfg)
