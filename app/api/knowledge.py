from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from app.auth.security import get_current_user_id
from app.config import settings
from app.rag.retriever import KnowledgeBaseNotReady, get_retriever
from app.rag.schema import ExerciseFilter

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class SearchRequest(ExerciseFilter):
    query: str = Field("", max_length=200)
    top_k: int = Field(6, ge=1, le=30)


def _retriever():
    try:
        return get_retriever()
    except KnowledgeBaseNotReady as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


@router.post("/search")
async def search(body: SearchRequest, _user: str = Depends(get_current_user_id)) -> dict[str, Any]:
    """检索调试接口：返回融合排序的名次信息，便于评估检索质量。"""
    from app.training.equipment import effective_equipment
    from app.users.profile import get_profile
    available = effective_equipment(await get_profile(_user))
    filters = body.to_filter()
    if available is not None:
        allowed = sorted(available & set(filters.equipment)) if filters.equipment else sorted(available)
        if not allowed:
            return {"mode": "filtered", "total_candidates": 0, "warnings": ["所选器械在当前场景不可用"], "hits": []}
        filters.equipment = allowed
    result = await _retriever().search(body.query, filters, body.top_k)
    return {
        "mode": result.mode,
        "total_candidates": result.total_candidates,
        "warnings": result.warnings,
        "hits": [
            {
                **h.doc.summary(),
                "score": h.score,
                "dense_rank": h.dense_rank,
                "sparse_rank": h.sparse_rank,
                "rerank_score": h.rerank_score,
            }
            for h in result.hits
        ],
    }


@router.get("/exercises/{exercise_id}")
async def exercise_detail(exercise_id: str, _user: str = Depends(get_current_user_id)) -> dict[str, Any]:
    doc = _retriever().get(exercise_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "动作不存在")
    return doc.detail(settings.data.image_base_url)


@router.get("/stats")
async def stats(_user: str = Depends(get_current_user_id)) -> dict[str, Any]:
    r = _retriever()
    return {
        "exercises": len(r.docs),
        "vector_index": bool(r.vector_store),
        "vector_meta": r.vector_store.meta if r.vector_store else None,
        "reranker": bool(r.reranker),
    }
