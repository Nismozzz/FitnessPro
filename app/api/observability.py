from typing import Literal

from fastapi import APIRouter, Depends, Query

from app.auth.security import get_current_user_id
from app.observability import service

router = APIRouter(prefix="/stats", tags=["observability"])
Scope = Literal["self", "all"]
Range = Literal["1d", "7d", "30d", "90d"]


@router.get("/llm")
async def llm_stats(range: Range = "7d", scope: Scope = "self", user_id: str = Depends(get_current_user_id)):
    return await service.llm_stats(user_id, scope, int(range[:-1]))


@router.get("/retrieval")
async def retrieval_stats(range: Range = "7d", scope: Scope = "self", user_id: str = Depends(get_current_user_id)):
    return await service.retrieval_stats(user_id, scope, int(range[:-1]))


@router.get("/requests")
async def requests(range: Range = "7d", scope: Scope = "self", limit: int = Query(30, ge=1, le=100),
                   user_id: str = Depends(get_current_user_id)):
    return await service.requests(user_id, scope, int(range[:-1]), limit)


@router.get("/requests/{request_id}")
async def request_detail(request_id: str, scope: Scope = "self", user_id: str = Depends(get_current_user_id)):
    return await service.request_detail(user_id, scope, request_id)
