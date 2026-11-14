from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.auth.security import get_current_user_id
from app.training import service
from app.training.schema import LogCreate, PlanDraft, Replacement
from app.training.stats import weekly_stats
from app.training.progression import ApplyNextWeek, apply_next_week, suggest_next_week

router = APIRouter(tags=["training"])
UserId = Annotated[str, Depends(get_current_user_id)]


@router.post("/plans/validate")
async def validate(body: PlanDraft, user_id: UserId):
    return await service.check_plan(user_id, body)


@router.get("/plans")
async def plans(user_id: UserId):
    return await service.list_plans(user_id)


@router.post("/plans", status_code=201)
async def create_plan(body: PlanDraft, user_id: UserId):
    return await service.save_plan(user_id, body)


@router.get("/plans/active")
async def active(user_id: UserId):
    return await service.active_plan(user_id)


@router.get("/plans/{plan_id}")
async def plan(plan_id: str, user_id: UserId):
    return await service.get_plan(user_id, plan_id)


@router.post("/plans/{plan_id}/activate")
async def activate(plan_id: str, user_id: UserId):
    return await service.activate_plan(user_id, plan_id)


@router.patch("/plans/{plan_id}/exercises")
async def replace(plan_id: str, body: Replacement, user_id: UserId):
    return await service.replace_exercise(user_id, plan_id, body)


@router.get("/logs")
async def logs(user_id: UserId):
    return await service.list_logs(user_id)


@router.post("/logs", status_code=201)
async def create_log(body: LogCreate, user_id: UserId):
    return await service.log_workout(user_id, body)


@router.get("/logs/{log_id}")
async def log(log_id: str, user_id: UserId):
    return await service.get_log(user_id, log_id)


@router.get("/stats/weekly")
async def stats(user_id: UserId, week_start: date = Query(...), plan_id: str | None = None):
    return await weekly_stats(user_id, week_start, plan_id)


@router.post("/plans/{plan_id}/suggest-next-week")
async def suggest(plan_id: str, user_id: UserId):
    return await suggest_next_week(user_id, plan_id)


@router.post("/plans/{plan_id}/next-week", status_code=201)
async def next_week(plan_id: str, body: ApplyNextWeek, user_id: UserId):
    return await apply_next_week(user_id, plan_id, body)
