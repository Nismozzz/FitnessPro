"""Record-driven progression: deterministic rules, read-only preview, confirmed draft creation."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import Field
from sqlalchemy import select

from app.config import settings
from app.database.models import PlanDay, WorkoutLog, WorkoutPlan, WorkoutSet
from app.database.session import SessionLocal
from app.rag.retriever import KnowledgeBaseNotReady, get_retriever
from app.rag.schema import ExerciseFilter, LEVEL_ORDER
from app.training import service
from app.training.schema import PlanDraft, StrictModel
from app.training.stats import epley
from app.training.validator import validate_plan

LOWER_BODY = {"quadriceps", "hamstrings", "glutes", "calves", "abductors", "adductors"}


class ReplacementChoice(StrictModel):
    weekday: int = Field(ge=0, le=6)
    position: int = Field(ge=0, le=14)
    exercise_id: str = Field(min_length=1, max_length=128)


class ApplyNextWeek(StrictModel):
    suggestion_id: str = Field(min_length=64, max_length=64)
    replacements: list[ReplacementChoice] = Field(default_factory=list, max_length=50)


def analyze_exercise(prescription, doc: dict, sessions: list[dict], completed_week_end: date) -> dict | None:
    """Sessions are chronological, with local date, sets, and optional historical prescription.

    Only two distinct training dates at the current prescription/load form a streak.
    Missing sets/weights are insufficient evidence, never assumed failed/completed.
    """
    p = prescription
    if p.duration_seconds is not None or doc.get("category") not in {"strength", "powerlifting", "olympic weightlifting", "strongman"}:
        return None
    latest_by_date = {s["date"]: s for s in sessions}
    recent = sorted(latest_by_date.values(), key=lambda s: s["date"])[-2:]
    weight = p.target_weight_kg

    def outcome(session):
        old = session.get("prescription")
        if old and any(old[k] != getattr(p, k) for k in ("sets", "reps_min", "reps_max", "target_weight_kg")):
            return "unknown"
        sets = session["sets"]
        if len(sets) != p.sets or {s["set_no"] for s in sets} != set(range(1, p.sets + 1)):
            return "unknown"
        if any(s["duration_seconds"] is not None for s in sets):
            return "unknown"
        if weight is not None and weight > 0 and any(s["weight_kg"] != weight for s in sets if s["completed"]):
            return "unknown"
        if all(s["completed"] and (s["reps"] or 0) >= p.reps_max for s in sets):
            return "success"
        if any(not s["completed"] or (s["reps"] is not None and s["reps"] < p.reps_min) for s in sets):
            return "failure"
        return "neutral"

    outcomes = [outcome(s) for s in recent]
    evidence = {"training_dates": [str(s["date"]) for s in recent], "outcomes": outcomes}
    if len(outcomes) == 2 and outcomes == ["failure", "failure"]:
        if weight is not None and weight > 0:
            return {"kind": "deload", "changes": {"target_weight_kg": round(weight * .925, 3)},
                    "reason": "连续两次训练未完成全部最低次数目标，下周降重 7.5%。", "evidence": evidence}
        return None
    if len(outcomes) == 2 and outcomes == ["success", "success"]:
        muscles = set(doc.get("primary_muscles") or [])
        if weight is not None and weight > 0 and doc.get("equipment") != "body only" and muscles:
            increment = 5 if muscles <= LOWER_BODY else 2.5
            if weight + increment <= 1000:
                return {"kind": "increase_weight", "changes": {"target_weight_kg": weight + increment},
                        "reason": f"连续两次完成全部组数并达到次数上限，下周加重 {increment:g} kg。", "evidence": evidence}
        elif p.reps_max < 100:
            return {"kind": "increase_reps", "changes": {"reps_min": p.reps_min + 1, "reps_max": p.reps_max + 1},
                    "reason": "连续两次完成全部组数并达到次数上限；徒手或未设定负重，次数区间加 1。", "evidence": evidence}

    # Three consecutive *completed* calendar weeks. Missing/partial weeks are not stagnation.
    weeks = [completed_week_end - timedelta(weeks=i) for i in (3, 2, 1)]
    weekly_best = defaultdict(float)
    for session in sessions:
        day = session["date"]
        week = day - timedelta(days=day.weekday())
        if week not in weeks:
            continue
        for entry in session["sets"]:
            estimate = epley(entry["weight_kg"], entry["reps"], doc.get("equipment")) if entry["completed"] else None
            if estimate:
                weekly_best[week] = max(weekly_best[week], estimate)
    if all(w in weekly_best for w in weeks):
        values = [weekly_best[w] for w in weeks]
        if values[1] <= values[0] + 1e-6 and values[2] <= max(values[:2]) + 1e-6:
            return {"kind": "replace", "changes": {}, "reason": "连续三个完整周的最佳估算 1RM 没有提升，建议考虑同主要肌群、同器械的其他动作。",
                    "evidence": {"weekly_estimated_1rm": [{"week_start": str(w), "kg": round(weekly_best[w], 2)} for w in weeks]}}
    return None


async def suggest_next_week(user_id: str, plan_id: str | None = None) -> dict:
    source = await service.get_plan(user_id, plan_id) if plan_id else await service.active_plan(user_id)
    if source is None:
        raise service.TrainingError("请先激活一份训练计划", 404)
    if source["status"] != "active":
        raise service.TrainingError("只能根据当前使用中的计划生成下周建议", 409)
    async with SessionLocal() as db:
        row = await service.owned_plan(db, user_id, source["id"])
        plan = PlanDraft.model_validate(row.draft)
        root = row.root_id
        # Include archived versions in this lineage; unrelated plans cannot drive progression.
        day_rows = (await db.execute(select(PlanDay.id, WorkoutPlan.draft, PlanDay.weekday).join(WorkoutPlan, PlanDay.plan_id == WorkoutPlan.id)
                                    .where(WorkoutPlan.user_id == user_id, WorkoutPlan.root_id == root))).all()
        historical = {did: next(d for d in data["days"] if d["weekday"] == weekday) for did, data, weekday in day_rows}
        tz = ZoneInfo(settings.timezone)
        next_week = plan.week_start + timedelta(days=7)
        now = datetime.now(timezone.utc)
        end = min(now, datetime.combine(next_week, time.min, tzinfo=tz).astimezone(timezone.utc))
        start = end - timedelta(days=28)
        logs = (await db.scalars(select(WorkoutLog).where(WorkoutLog.user_id == user_id, WorkoutLog.performed_at >= start.replace(tzinfo=None),
                                                        WorkoutLog.performed_at < end.replace(tzinfo=None))
                                 .order_by(WorkoutLog.performed_at, WorkoutLog.id))).all()
        entries = (await db.scalars(select(WorkoutSet).where(WorkoutSet.log_id.in_([log.id for log in logs])))).all() if logs else []
    by_log = defaultdict(lambda: defaultdict(list))
    for entry in entries:
        by_log[entry.log_id][entry.exercise_id].append({k: getattr(entry, k) for k in ("set_no", "reps", "duration_seconds", "weight_kg", "completed")})
    history = defaultdict(list)
    for log in logs:
        if log.plan_day_id and log.plan_day_id not in historical:
            continue
        day = historical.get(log.plan_day_id)
        for eid, sets in by_log[log.id].items():
            old = next((e for e in day["exercises"] if e["exercise_id"] == eid), None) if day else None
            history[eid].append({"date": log.performed_at.replace(tzinfo=timezone.utc).astimezone(tz).date(), "sets": sets, "prescription": old})
    profile, docs = await service.validation_inputs(user_id, plan)
    proposed = plan.model_copy(deep=True)
    proposed.week_start = next_week
    local_end = end.astimezone(tz).date()
    completed_week_end = local_end - timedelta(days=local_end.weekday())
    adjustments, warnings = [], []
    for day in proposed.days:
        for position, exercise in enumerate(day.exercises):
            doc = docs.get(exercise.exercise_id, {})
            result = analyze_exercise(exercise, doc, history[exercise.exercise_id], completed_week_end)
            if not result:
                continue
            item = {"weekday": day.weekday, "position": position, "exercise_id": exercise.exercise_id,
                    "name": doc.get("name_zh") or doc.get("name") or exercise.exercise_id, **result, "candidates": []}
            if result["kind"] == "replace" and doc.get("primary_muscles") and doc.get("equipment"):
                filters = ExerciseFilter(primary_muscles=doc["primary_muscles"], equipment=[doc["equipment"]],
                                         levels=[k for k, v in LEVEL_ORDER.items() if v <= LEVEL_ORDER.get(profile.get("experience_level"), 0) + 1],
                                         categories=[doc["category"]], exclude_ids=[e.exercise_id for e in day.exercises])
                try:
                    hits = await get_retriever().search("", filters, 15)
                    for hit in hits.hits:
                        if not filters.matches(hit.doc):
                            continue
                        # Check the entire resulting week, including injury warnings and current equipment.
                        candidate_plan = proposed.model_copy(deep=True)
                        candidate = candidate_plan.days[proposed.days.index(day)].exercises[position]
                        candidate.exercise_id = hit.doc.id
                        candidate.target_weight_kg = None  # Loads do not transfer between different movements.
                        candidate_docs = {**docs, hit.doc.id: {k: getattr(hit.doc, k) for k in ("id", "name", "name_zh", "equipment", "category", "level", "primary_muscles", "secondary_muscles")}}
                        check = validate_plan(candidate_plan, profile, candidate_docs)
                        if check["valid"] and not any(w["code"] == "injury_risk" and w.get("exercise_id") == hit.doc.id for w in check["warnings"]):
                            item["candidates"].append({"exercise_id": hit.doc.id, "name": hit.doc.display_name})
                        if len(item["candidates"]) == 3:
                            break
                except KnowledgeBaseNotReady:
                    warnings.append("动作知识库尚未就绪，暂时无法检索替换候选；原动作保留。")
                if not item["candidates"]:
                    warnings.append(f"{item['name']}未找到通过校验的替换候选，保留原动作，可让教练进一步评估。")
            for key, value in result["changes"].items():
                setattr(exercise, key, value)
            adjustments.append(item)
    data = proposed.model_dump(mode="json")
    validation = await service.check_plan(user_id, proposed)
    result = {"source_plan_id": source["id"], "source_version": source["version"], "week_start": str(next_week),
              "adjustments": adjustments, "proposed_plan": data, "validation": validation, "warnings": sorted(set(warnings)),
              "notes": ["建议不会修改当前计划；确认后保存下周草稿，激活时旧计划归档。", "加减重仅比较当前处方及负重的最近两次训练（不同日期）；缺组或未知重量不视为失败。", "停滞按三个连续完整周的最佳估算1RM判断；仅使用1–10次自由负重完成组，缺周不判断。", "替换动作不沿用旧动作重量，请重新确定合适负重。"]}
    result["suggestion_id"] = service.digest({"user_id": user_id, "source": source["id"], "result": result,
                                              "history": [{"id": log.id, "hash": log.content_hash} for log in logs]})
    return result


async def apply_next_week(user_id: str, plan_id: str, body: ApplyNextWeek) -> dict:
    suggestion = await suggest_next_week(user_id, plan_id)
    if suggestion["suggestion_id"] != body.suggestion_id:
        raise service.TrainingError("记录、档案或建议已变化，请重新查看并确认下周建议", 409)
    plan = PlanDraft.model_validate(suggestion["proposed_plan"])
    seen = set()
    for choice in body.replacements:
        key = (choice.weekday, choice.position)
        if key in seen:
            raise service.TrainingError("同一位置不能重复替换", 422)
        seen.add(key)
        item = next((a for a in suggestion["adjustments"] if (a["weekday"], a["position"]) == key and a["kind"] == "replace"), None)
        if item is None or choice.exercise_id not in {c["exercise_id"] for c in item["candidates"]}:
            raise service.TrainingError("请选择本次建议中的替换候选", 422)
        day = next(d for d in plan.days if d.weekday == choice.weekday)
        day.exercises[choice.position].exercise_id = choice.exercise_id
        day.exercises[choice.position].target_weight_kg = None
    plan = PlanDraft.model_validate(plan.model_dump())
    return await service.save_plan(user_id, plan, parent_id=plan_id, require_active_parent=True)
