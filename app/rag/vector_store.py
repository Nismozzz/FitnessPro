"""
本地向量库：numpy 矩阵 + 暴力内积检索。

知识库约 900 条，暴力检索耗时在毫秒级，不需要 ANN 索引。接口与 Milvus / pgvector
的用法保持一致（向量检索 + 候选集合过滤），后续数据量变大时可以替换实现。
索引使用 .npy + JSON 存储（不使用 pickle）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

VECTORS_FILE = "vectors.npy"
IDS_FILE = "ids.json"
META_FILE = "meta.json"


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class LocalVectorStore:
    def __init__(self, ids: list[str], vectors: np.ndarray, meta: dict[str, Any] | None = None):
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[0] != len(ids):
            raise ValueError("向量矩阵维度与 id 数量不一致")
        self.ids = list(ids)
        self.vectors = l2_normalize(vectors)
        self.meta = meta or {}

    def __len__(self) -> int:
        return len(self.ids)

    @property
    def dim(self) -> int:
        return int(self.vectors.shape[1])

    def search(
        self, query_vector: np.ndarray, top_k: int, allowed: set[str] | None = None
    ) -> list[tuple[str, float]]:
        if not self.ids or top_k <= 0:
            return []
        q = l2_normalize(np.asarray(query_vector, dtype=np.float32).reshape(-1))
        if q.shape[0] != self.dim:
            raise ValueError(f"查询向量维度 {q.shape[0]} 与索引维度 {self.dim} 不一致")
        scores = self.vectors @ q
        if allowed is not None:
            mask = np.fromiter((i in allowed for i in self.ids), dtype=bool, count=len(self.ids))
            scores = np.where(mask, scores, -np.inf)
        k = min(top_k, len(self.ids))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(self.ids[i], float(scores[i])) for i in top if np.isfinite(scores[i])]

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        np.save(directory / VECTORS_FILE, self.vectors)
        (directory / IDS_FILE).write_text(json.dumps(self.ids, ensure_ascii=False), encoding="utf-8")
        meta = {**self.meta, "count": len(self.ids), "dim": self.dim}
        (directory / META_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, directory: Path) -> "LocalVectorStore | None":
        if not (directory / VECTORS_FILE).exists():
            return None
        vectors = np.load(directory / VECTORS_FILE, allow_pickle=False)
        ids = json.loads((directory / IDS_FILE).read_text(encoding="utf-8"))
        meta_path = directory / META_FILE
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        return cls(ids, vectors, meta)
