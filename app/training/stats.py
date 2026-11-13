from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.database.models import PlanDay, PlanExercise, WorkoutLog, WorkoutPlan, WorkoutSet
from app.database.session import SessionLocal
from app.training.service import TrainingError, owned_plan
from app.training.validator import muscle_credits


def epley(weight: float | None, reps: int | None, equipment: str | None) -> float | None:
    if weight is None or weight <= 0 or reps is None or not 1 <= reps <= 10:
        return None
    if equipment not in {"barbell", "dumbbell", "kettlebells", "e-z curl bar"}:
        return None
    return weight if reps == 1 else weight * (1 + reps / 30)


async def weekly_stats(user_id: str, week_start: date, plan_id: str | None = None) -> dict:
    if week_start.weekday() != 0:
        raise TrainingError("week_start 必须为周一", 422)
    tz = ZoneInfo(settings.timezone)
    start = datetime.combine(week_start, time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    end = datetime.combine(week_start + timedelta(days=7), time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    async with SessionLocal() as db:
        if plan_id:
            plan = await owned_plan(db, user_id, plan_id)
            if plan.week_start != week_start:
                raise TrainingError("计划不属于查询周", 422)
        else:
            plan = await db.scalar(select(WorkoutPlan).where(WorkoutPlan.user_id == user_id, WorkoutPlan.week_start == week_start)
                                   .order_by((WorkoutPlan.status == "active").desc(), (WorkoutPlan.status == "archived").desc(), WorkoutPlan.created_at.desc()))
        # Never mix drafts into completion denominators automatically.
        if plan and plan.status == "draft" and not plan_id:
            plan = None
        days = (await db.scalars(select(PlanDay).where(PlanDay.plan_id == plan.id))).all() if plan else []
        planned, expected = defaultdict(float), {}
        for day in days:
            entries = (await db.scalars(select(PlanExercise).where(PlanExercise.plan_day_id == day.id))).all()
            expected[day.id] = {e.exercise_id: e.sets for e in entries}
            for e in entries:
                if e.snapshot.get("category") not in {"stretching", "cardio"}:
                    for muscle, factor in muscle_credits(e.snapshot).items():
                        planned[muscle] += e.sets * factor
        logs = (await db.scalars(select(WorkoutLog).where(WorkoutLog.user_id == user_id, WorkoutLog.performed_at >= start, WorkoutLog.performed_at < end))).all()
        actual, volume_by_muscle = defaultdict(float), defaultdict(float)
        daily = {str(week_start + timedelta(days=i)): {"date": str(week_start + timedelta(days=i)), "volume_kg_reps": 0.0, "duration_min": 0, "sets": 0} for i in range(7)}
        done_counts = defaultdict(lambda: defaultdict(int))
        best, best_before = {}, {}
        total_volume, completed_sets = 0.0, 0
        all_rows = (await db.execute(select(WorkoutSet, WorkoutLog.performed_at).join(WorkoutLog, WorkoutSet.log_id == WorkoutLog.id)
                                     .where(WorkoutLog.user_id == user_id, WorkoutLog.performed_at < end, WorkoutSet.completed.is_(True)))).all()
        for entry, when in all_rows:
            estimate = epley(entry.weight_kg, entry.reps, entry.snapshot.get("equipment"))
            if estimate:
                dest = best_before if when < start else best
                old = dest.get(entry.exercise_id, {}).get("estimated_1rm_kg", 0)
                if estimate > old:
                    dest[entry.exercise_id] = {"exercise_id": entry.exercise_id, "name": entry.snapshot.get("name_zh") or entry.snapshot.get("name"), "estimated_1rm_kg": round(estimate, 2)}
        for log in logs:
            date_key = str(log.performed_at.replace(tzinfo=timezone.utc).astimezone(tz).date())
            daily[date_key]["duration_min"] += log.duration_min
            entries = (await db.scalars(select(WorkoutSet).where(WorkoutSet.log_id == log.id, WorkoutSet.completed.is_(True)))).all()
            for e in entries:
                completed_sets += 1
                daily[date_key]["sets"] += 1
                if log.plan_day_id in expected:
                    done_counts[log.plan_day_id][e.exercise_id] += 1
                volume = (e.weight_kg or 0) * (e.reps or 0)
                if e.snapshot.get("equipment") == "body only":
                    volume = 0.0
                total_volume += volume
                daily[date_key]["volume_kg_reps"] += volume
                credits = muscle_credits(e.snapshot)
                if e.snapshot.get("category") not in {"stretching", "cardio"}:
                    for muscle, factor in credits.items():
                        actual[muscle] += factor
                # Distribute external-load volume instead of counting it once per muscle.
                divisor = sum(credits.values()) or 1
                for muscle, factor in credits.items():
                    volume_by_muscle[muscle] += volume * factor / divisor
        finished = sum(all(done_counts[day.id][eid] >= count for eid, count in expected[day.id].items()) for day in days)
        prs = []
        for eid, entry in best.items():
            previous = best_before.get(eid, {}).get("estimated_1rm_kg")
            prs.append({**entry, "previous_best_kg": previous, "is_pr": previous is None or entry["estimated_1rm_kg"] > previous})
        return {"week_start": str(week_start), "plan_id": plan.id if plan else None,
                "planned_days": len(days), "completed_days": finished,
                "completion_rate": round(finished / len(days), 4) if days else None,
                "workouts": len(logs), "completed_sets": completed_sets,
                "duration_min": sum(x["duration_min"] for x in daily.values()),
                "volume_kg_reps": round(total_volume, 2), "daily": list(daily.values()),
                "muscles": [{"muscle": m, "planned_sets": round(planned[m], 1), "actual_sets": round(actual[m], 1), "volume_kg_reps": round(volume_by_muscle[m], 2)} for m in sorted(set(planned) | set(actual))],
                "prs": prs,
                "notes": ["完成率按所选计划版本的训练日去重；完成该日所有计划组数后计为完成，周内补练可计入。", "容量只统计完成组的外加重量×次数，徒手与计时训练不估算体重负荷。", "估算1RM仅针对1–10次的自由负重动作；哑铃重量请始终使用相同口径。"]}
