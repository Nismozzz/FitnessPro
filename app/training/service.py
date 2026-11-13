from __future__ import annotations

import hashlib
import json
from datetime import timezone
from typing import Any

from sqlalchemy import func, select, update

from app.database.models import Exercise, PlanDay, PlanExercise, User, WorkoutLog, WorkoutPlan, WorkoutSet, new_id
from app.database.session import SessionLocal
from app.memory.service import memory_service
from app.training.schema import LogCreate, PlanDraft, Replacement
from app.training.validator import validate_plan
from app.users.profile import get_profile


class TrainingError(Exception):
    def __init__(self, message: str, status: int = 400, validation: dict | None = None):
        super().__init__(message)
        self.status = status
        self.validation = validation


def digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def snapshot(exercise: Exercise) -> dict:
    return {"id": exercise.id, "name": exercise.name, "name_zh": exercise.name_zh,
            "equipment": exercise.equipment, "category": exercise.category, "level": exercise.level,
            "primary_muscles": exercise.primary_muscles, "secondary_muscles": exercise.secondary_muscles}


async def catalog(ids: set[str]) -> dict[str, dict]:
    async with SessionLocal() as db:
        rows = (await db.scalars(select(Exercise).where(Exercise.id.in_(ids)))).all()
    return {row.id: snapshot(row) for row in rows}


async def validation_inputs(user_id: str, plan: PlanDraft):
    profile = await get_profile(user_id)
    active, _ = await memory_service.for_prompt(user_id)
    profile["injuries"] = list(profile.get("injuries") or []) + [m.content for m in active if m.kind == "injury"]
    docs = await catalog({e.exercise_id for d in plan.days for e in d.exercises})
    return profile, docs


async def check_plan(user_id: str, plan: PlanDraft) -> dict:
    profile, docs = await validation_inputs(user_id, plan)
    return validate_plan(plan, profile, docs)


def canonical(plan: PlanDraft) -> dict:
    data = plan.model_dump(mode="json")
    data["days"].sort(key=lambda d: d["weekday"])
    data["target_muscles"] = sorted(set(data["target_muscles"]))
    return data


async def lock_user(db, user_id: str):
    # Serialize this user's writes on SQLite and PostgreSQL, including activation races.
    await db.execute(update(User).where(User.id == user_id).values(username=User.username))


async def owned_plan(db, user_id: str, plan_id: str) -> WorkoutPlan:
    row = await db.scalar(select(WorkoutPlan).where(WorkoutPlan.id == plan_id, WorkoutPlan.user_id == user_id))
    if row is None:
        raise TrainingError("计划不存在", 404)
    return row


async def serialize_plan(db, row: WorkoutPlan) -> dict:
    data = {"id": row.id, "name": row.name, "goal": row.goal, "split_type": row.split_type,
            "week_start": row.week_start.isoformat(), "status": row.status, "version": row.version,
            "root_id": row.root_id, "created_at": row.created_at.isoformat(),
            "target_muscles": row.draft.get("target_muscles", []), "validation": row.validation, "days": []}
    days = (await db.scalars(select(PlanDay).where(PlanDay.plan_id == row.id).order_by(PlanDay.weekday))).all()
    for day in days:
        exercises = (await db.scalars(select(PlanExercise).where(PlanExercise.plan_day_id == day.id).order_by(PlanExercise.position))).all()
        data["days"].append({"id": day.id, "weekday": day.weekday, "focus": day.focus, "notes": day.notes,
                             "warmup_minutes": day.warmup_minutes, "cooldown_minutes": day.cooldown_minutes,
                             "exercises": [{**{k: getattr(e, k) for k in ("id", "exercise_id", "position", "sets", "reps_min", "reps_max", "duration_seconds", "rest_seconds", "rpe", "target_weight_kg", "notes")}, "exercise": e.snapshot} for e in exercises]})
    return data


async def save_plan(user_id: str, plan: PlanDraft, parent_id: str | None = None) -> dict:
    profile, docs = await validation_inputs(user_id, plan)
    validation = validate_plan(plan, profile, docs)
    if not validation["valid"]:
        raise TrainingError("计划未通过校验", 422, validation)
    data = canonical(plan)
    content_hash = digest(data)
    async with SessionLocal() as db:
        await lock_user(db, user_id)
        parent = await owned_plan(db, user_id, parent_id) if parent_id else None
        existing = await db.scalar(select(WorkoutPlan).where(WorkoutPlan.user_id == user_id, WorkoutPlan.content_hash == content_hash))
        if existing:
            return await serialize_plan(db, existing)
        pid = new_id()
        root = parent.root_id if parent else pid
        version = (await db.scalar(select(func.max(WorkoutPlan.version)).where(WorkoutPlan.user_id == user_id, WorkoutPlan.root_id == root)) or 0) + 1
        row = WorkoutPlan(id=pid, user_id=user_id, name=plan.name, goal=plan.goal,
                          split_type=plan.split_type, week_start=plan.week_start, root_id=root,
                          version=version, content_hash=content_hash, draft=data, validation=validation)
        db.add(row)
        await db.flush()
        for day in plan.days:
            dayrow = PlanDay(plan_id=pid, **day.model_dump(exclude={"exercises"}))
            db.add(dayrow)
            await db.flush()
            for position, exercise in enumerate(day.exercises):
                db.add(PlanExercise(plan_day_id=dayrow.id, position=position, **exercise.model_dump(), snapshot=docs[exercise.exercise_id]))
        await db.flush()
        result = await serialize_plan(db, row)
        await db.commit()
        return result


async def list_plans(user_id: str) -> list[dict]:
    async with SessionLocal() as db:
        rows = (await db.scalars(select(WorkoutPlan).where(WorkoutPlan.user_id == user_id).order_by(WorkoutPlan.created_at.desc()))).all()
        return [await serialize_plan(db, row) for row in rows]


async def get_plan(user_id: str, plan_id: str) -> dict:
    async with SessionLocal() as db:
        return await serialize_plan(db, await owned_plan(db, user_id, plan_id))


async def active_plan(user_id: str) -> dict | None:
    async with SessionLocal() as db:
        row = await db.scalar(select(WorkoutPlan).where(WorkoutPlan.user_id == user_id, WorkoutPlan.status == "active"))
        return await serialize_plan(db, row) if row else None


async def activate_plan(user_id: str, plan_id: str) -> dict:
    async with SessionLocal() as db:
        await lock_user(db, user_id)
        row = await owned_plan(db, user_id, plan_id)
        plan = PlanDraft.model_validate(row.draft)
        result = await check_plan(user_id, plan)
        if not result["valid"]:
            raise TrainingError("当前训练条件下计划未通过校验", 422, result)
        await db.execute(update(WorkoutPlan).where(WorkoutPlan.user_id == user_id, WorkoutPlan.status == "active", WorkoutPlan.id != row.id).values(status="archived"))
        row.status, row.validation = "active", result
        await db.flush()
        result = await serialize_plan(db, row)
        await db.commit()
        return result


async def replace_exercise(user_id: str, plan_id: str, replacement: Replacement) -> dict:
    async with SessionLocal() as db:
        row = await owned_plan(db, user_id, plan_id)
        plan = PlanDraft.model_validate(row.draft)
    day = next((d for d in plan.days if d.weekday == replacement.weekday), None)
    if day is None or replacement.position >= len(day.exercises):
        raise TrainingError("计划动作不存在", 404)
    day.exercises[replacement.position] = replacement.exercise
    plan = PlanDraft.model_validate(plan.model_dump())
    return await save_plan(user_id, plan, parent_id=plan_id)


async def serialize_log(db, row: WorkoutLog) -> dict:
    sets = (await db.scalars(select(WorkoutSet).where(WorkoutSet.log_id == row.id).order_by(WorkoutSet.exercise_id, WorkoutSet.set_no))).all()
    return {"id": row.id, "plan_day_id": row.plan_day_id, "performed_at": row.performed_at.replace(tzinfo=timezone.utc).isoformat(),
            "duration_min": row.duration_min, "rpe": row.rpe, "notes": row.notes,
            "sets": [{**{k: getattr(s, k) for k in ("exercise_id", "set_no", "reps", "duration_seconds", "weight_kg", "rpe", "completed")}, "exercise": s.snapshot} for s in sets]}


async def log_workout(user_id: str, body: LogCreate) -> dict:
    normalized = body.model_copy(update={"performed_at": body.performed_at.astimezone(timezone.utc)})
    data = normalized.model_dump(mode="json", exclude={"idempotency_key"})
    content_hash = digest(data)
    async with SessionLocal() as db:
        await lock_user(db, user_id)
        existing = await db.scalar(select(WorkoutLog).where(WorkoutLog.user_id == user_id, WorkoutLog.idempotency_key == body.idempotency_key))
        if existing:
            if existing.content_hash != content_hash:
                raise TrainingError("同一记录键已用于不同内容，请刷新后重试", 409)
            return await serialize_log(db, existing)
        expected = {}
        if body.plan_day_id:
            day = await db.get(PlanDay, body.plan_day_id)
            if day is None:
                raise TrainingError("计划日不存在", 404)
            await owned_plan(db, user_id, day.plan_id)
            expected = {e.exercise_id: e for e in (await db.scalars(select(PlanExercise).where(PlanExercise.plan_day_id == day.id))).all()}
        docs = {e.id: snapshot(e) for e in (await db.scalars(select(Exercise).where(Exercise.id.in_({s.exercise_id for s in body.sets})))).all()}
        for entry in body.sets:
            if entry.exercise_id not in docs:
                raise TrainingError("记录包含不存在的动作", 422)
            if body.plan_day_id and entry.exercise_id not in expected:
                raise TrainingError("动作不属于所选计划日，额外训练请单独记录", 422)
            if entry.completed and expected:
                planned = expected[entry.exercise_id]
                if (planned.duration_seconds is None) != (entry.duration_seconds is None):
                    raise TrainingError("记录的次数/计时类型与计划不一致", 422)
        row = WorkoutLog(user_id=user_id, **body.model_dump(exclude={"sets", "performed_at"}),
                         performed_at=normalized.performed_at.replace(tzinfo=None), content_hash=content_hash)
        db.add(row)
        await db.flush()
        for entry in body.sets:
            db.add(WorkoutSet(log_id=row.id, **entry.model_dump(), snapshot=docs[entry.exercise_id]))
        await db.flush()
        result = await serialize_log(db, row)
        await db.commit()
        return result


async def list_logs(user_id: str) -> list[dict]:
    async with SessionLocal() as db:
        rows = (await db.scalars(select(WorkoutLog).where(WorkoutLog.user_id == user_id).order_by(WorkoutLog.performed_at.desc()).limit(300))).all()
        return [await serialize_log(db, row) for row in rows]


async def get_log(user_id: str, log_id: str) -> dict:
    async with SessionLocal() as db:
        row = await db.scalar(select(WorkoutLog).where(WorkoutLog.id == log_id, WorkoutLog.user_id == user_id))
        if row is None:
            raise TrainingError("训练记录不存在", 404)
        return await serialize_log(db, row)
