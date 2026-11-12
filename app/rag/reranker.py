"""外部重排（SiliconFlow / Jina / Cohere 风格的 /rerank 接口）。"""

from __future__ import annotations

import httpx

from app.config import RerankerSettings


class Reranker:
    def __init__(self, cfg: RerankerSettings):
        if not cfg.api_key:
            raise RuntimeError(f"Reranker 未配置 API Key（环境变量 {cfg.api_key_env}）")
        self.cfg = cfg

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """返回 [(documents 中的下标, 相关度)]，按相关度降序。"""
        payload = {
            "model": self.cfg.model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
            "return_documents": False,
        }
        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            resp = await client.post(
                self.cfg.base_url,
                json=payload,
                headers={"Authorization": f"Bearer {self.cfg.api_key}"},
            )
            resp.raise_for_status()
        results = resp.json().get("results", [])
        ranked = [(int(r["index"]), float(r["relevance_score"])) for r in results]
        return sorted(ranked, key=lambda x: -x[1])
