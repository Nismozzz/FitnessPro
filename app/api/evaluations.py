from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.security import get_current_user_id
from app.evaluation import service as svc

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


@router.get("")
async def list_evaluations(
    session_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
) -> list[dict[str, Any]]:
    return [svc.evaluation_to_dict(e) for e in await svc.list_evaluations(user_id, limit, session_id)]


@router.get("/health")
async def health(_user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    return svc.health()


@router.get("/statistics")
async def statistics(user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    return await svc.statistics(user_id)


@router.get("/trends")
async def trends(
    days: int = Query(7, ge=1, le=90),
    granularity: Literal["day", "hour"] = "day",
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    return await svc.trends(user_id, days, granularity)


@router.get("/alerts")
async def alerts(limit: int = Query(20, ge=1, le=100), user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    return await svc.alerts(user_id, limit)


@router.get("/{evaluation_id}")
async def get_evaluation(evaluation_id: str, user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    row = await svc.get_evaluation(user_id, evaluation_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "评估记录不存在")
    return svc.evaluation_to_dict(row, detail=True)
