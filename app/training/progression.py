"""根据实际完成组生成下一周处方。

双进阶：每一组都达到次数上限，并且达到目标重量后，才增加负重；次数区间保持不变。
徒手和弹力带做到上限后加次数，次数已到该目标的上限则加一组。
完不成计划组数或低于下限则减量。任一组 RPE ≥ 9.5 时不加重。
最近 3 次训练的最好一组都没有提高，记为停滞。
"""

from __future__ import annotations

import copy
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.rag.schema import LEVEL_ORDER
from app.training.schema import PlanDraft
from app.training.validator import validate_plan

LOAD_STEP = {
    "barbell": 2.5,
    "e-z curl bar": 2.5,
    "dumbbell": 2.0,
    "kettlebells": 4.0,
    "machine": 2.5,
    "cable": 2.5,
}
REP_CAP = {
    "strength": 6,
    "muscle_gain": 12,
    "endurance": 20,
    "fat_loss": 20,
    "general_fitness": 15,
    "rehab": 15,
}
REP_FLOOR = {
    "strength": 3,
    "muscle_gain": 6,
    "endurance": 12,
    "fat_loss": 8,
    "general_fitness": 8,
    "rehab": 6,
}
MAX_SETS = 5
DURATION_STEP = 5
DURATION_FLOOR = 10
DURATION_CAP = 180
HIGH_RPE = 9.5
VIEW_FIELDS = ("sets", "reps_min", "reps_max", "duration_seconds", "target_weight_kg")

RULES = [
    "双进阶：同一动作的每一组都达到次数上限，并且达到目标重量后，才增加负重；次数区间保持不变。",
    "徒手或弹力带做到次数上限后加一次数；次数已到该目标的上限则加一组，最多 5 组。",
    "未完成计划组数，或次数、时长低于目标，则降低重量、次数或组数。",
    "任一组 RPE 达到 9.5 时，即使做到上限也不加重。",
    "最近 3 次训练的最好一组都没有提高，记为停滞，并给出同器械、同主肌群的替换候选。",
    "本周没有完成记录的动作维持原处方。这里只生成草稿，不会保存或激活。",
]


def counts_for_day(
    performed_at: datetime,
    plan_day_id: str | None,
    day_id: str,
    weekday: int,
    week_start: date,
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
) -> bool:
    """计划日上的记录优先；未关联计划的记录只计入它实际发生的那个星期。"""
    if not start <= performed_at < end:
        return False
    if plan_day_id == day_id:
        return True
    if plan_day_id:
        return False
    local_date = performed_at.replace(tzinfo=timezone.utc).astimezone(tz).date()
    return local_date == week_start + timedelta(days=weekday)


def session_best(sets: list[dict], timed: bool) -> tuple | None:
    done = _successful(sets)
    if timed:
        values = [entry["duration_seconds"] for entry in done if entry.get("duration_seconds")]
        return (max(values),) if values else None
    best = None
    for entry in done:
        if not entry.get("reps"):
            continue
        key = (float(entry["weight_kg"] or 0), int(entry["reps"]))
        if best is None or key > best:
            best = key
    return best


def build_performances(
    plan: PlanDraft,
    day_ids: dict[int, str],
    rows: list[dict],
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
) -> dict[tuple[int, int], dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["exercise_id"]].append(row)
    result = {}
    for day in plan.days:
        day_id = day_ids[day.weekday]
        for index, exercise in enumerate(day.exercises):
            logs: dict[str, list[dict]] = defaultdict(list)
            meta: dict[str, tuple] = {}
            for item in grouped.get(exercise.exercise_id, []):
                if item["performed_at"] >= end:
                    continue
                logs[item["log_id"]].append(item)
                meta[item["log_id"]] = (item["performed_at"], item.get("plan_day_id"))
            history = []
            completed = []
            for log_id in sorted(logs, key=lambda item: (meta[item][0], item)):
                entries = logs[log_id]
                best = session_best(entries, exercise.duration_seconds is not None)
                if best is not None:
                    history.append(best)
                performed_at, plan_day_id = meta[log_id]
                if counts_for_day(performed_at, plan_day_id, day_id, day.weekday, plan.week_start, start, end, tz):
                    completed.extend(entries)
            result[(day.weekday, index)] = {"completed": completed, "history": history}
    return result


def is_stalled(history: list[tuple]) -> bool:
    """最近 3 次的最好一组没有超过这 3 次里更早的两次。"""
    if len(history) < 3:
        return False
    first, middle, last = history[-3:]
    return last <= first and last <= middle


def rank_replacements(source: dict, rows: list[dict], level: str) -> list[dict]:
    muscles = set(source.get("primary_muscles") or [])
    limit = LEVEL_ORDER.get(level, 0) + 1
    source_level = LEVEL_ORDER.get(source.get("level"), 0)
    picked = []
    for row in rows:
        if row.get("id") == source.get("id") or row.get("equipment") != source.get("equipment"):
            continue
        if source.get("category") and row.get("category") != source.get("category"):
            continue
        if not muscles & set(row.get("primary_muscles") or []):
            continue
        if LEVEL_ORDER.get(row.get("level"), 99) > limit:
            continue
        picked.append(row)
    picked.sort(key=lambda row: (LEVEL_ORDER.get(row.get("level"), 99) != source_level, row.get("name") or ""))
    return picked[:3]


def prescribe(prescription: dict, equipment: str, goal: str, completed: list[dict], history: list[tuple]) -> dict:
    updated = _copy(prescription)
    done = _successful(completed)
    kind = _classify(updated, completed, done, equipment)
    action, reason = _apply(updated, kind, equipment, goal, done)
    plateau = bool(completed) and is_stalled(history)
    stalled = plateau and action == "hold" and kind in {"in_range", "top"}
    if stalled and kind == "in_range":
        reason += "最近 3 次最好成绩没有提高，可替换同器械动作，或继续把每组做到次数上限。"
    elif stalled:
        reason += "最近 3 次最好成绩没有提高，当前组次已到上限，建议替换同器械动作。"
    elif plateau and action in {"increase_load", "increase_reps", "increase_duration", "add_set"}:
        reason += "最近 3 次最好成绩没有提高，所以这次提高要求。"
    return {
        "action": action,
        "reason": reason,
        "stalled": stalled,
        "before": _view(prescription),
        "after": _view(updated),
        "prescription": updated,
    }


def progress_plan(
    plan: PlanDraft,
    performances: dict[tuple[int, int], dict],
    docs: dict[str, dict],
    profile: dict,
    candidates: dict[str, list[dict]],
    replace_stalled: bool,
    target_week: date,
) -> dict:
    data = plan.model_dump()
    data["week_start"] = target_week
    decisions = []
    for day in data["days"]:
        for index, exercise in enumerate(day["exercises"]):
            perf = performances.get((day["weekday"], index), {})
            doc = docs.get(exercise["exercise_id"], {})
            decision = prescribe(
                exercise,
                doc.get("equipment") or "",
                plan.goal,
                perf.get("completed") or [],
                perf.get("history") or [],
            )
            updated = decision.pop("prescription")
            from_id = exercise["exercise_id"]
            options = [_public_candidate(row) for row in candidates.get(from_id, [])] if decision["stalled"] else []
            if decision["stalled"] and replace_stalled and options:
                choice = options[0]
                updated = {**updated, "exercise_id": choice["id"]}
                decision["action"] = "replace"
                decision["after"] = _view(updated)
                label = choice.get("name_zh") or choice["name"]
                decision["reason"] += f" 已写入替换动作{label}，组次和重量保持不变。"
            decision.update({
                "weekday": day["weekday"],
                "position": index,
                "from_exercise_id": from_id,
                "exercise_id": updated["exercise_id"],
                "name": doc.get("name_zh") or doc.get("name") or from_id,
                "replacement_candidates": options,
                "_original": exercise,
                "_source_name": doc.get("name_zh") or doc.get("name") or from_id,
            })
            if decision["action"] == "replace":
                decision["name"] = options[0].get("name_zh") or options[0]["name"]
            day["exercises"][index] = updated
            decisions.append(decision)

    draft, validation = _restore_until_valid(data, decisions, profile, docs)
    for decision in decisions:
        decision.pop("_original", None)
        decision.pop("_source_name", None)
    return {"draft": draft.model_dump(mode="json"), "decisions": decisions, "validation": validation}


def _classify(prescription: dict, completed: list[dict], done: list[dict], equipment: str) -> str:
    if not completed:
        return "none"
    if len(done) < prescription["sets"]:
        return "short"
    if prescription.get("duration_seconds") is not None:
        target = prescription["duration_seconds"]
        if any((entry.get("duration_seconds") or 0) < target for entry in done):
            return "short"
        return "hard" if _hard(done) else "top"
    if any(entry.get("reps") is None or entry["reps"] < prescription["reps_min"] for entry in done):
        return "short"
    if any(entry["reps"] < prescription["reps_max"] for entry in done):
        return "in_range"
    if _hard(done):
        return "hard"
    if equipment in LOAD_STEP:
        weights = _weights(done)
        if weights is None:
            return "top_no_weight"
        target = prescription.get("target_weight_kg")
        if target is not None and min(weights) + 1e-9 < float(target):
            return "below_load"
    return "top"


def _apply(updated: dict, kind: str, equipment: str, goal: str, completed: list[dict]) -> tuple[str, str]:
    if kind == "none":
        return "hold", "本周没有这条动作的完成记录，维持原处方。"
    if kind == "hard":
        return "hold", "已完成计划，但有组次达到 RPE 9.5，先维持负荷。"
    if kind == "top_no_weight":
        return "hold", "次数已到上限，但缺少完整的重量记录，补记后再加重。"
    if kind == "below_load":
        return "hold", "次数已到上限，但最低完成重量低于目标，维持目标重量。"
    if kind == "in_range":
        return "hold", "各组都完成了，但还没有全部达到次数上限，维持原处方。"
    if kind == "top":
        return _advance(updated, equipment, goal, completed)
    return _reduce(updated, equipment, goal, completed)


def _advance(updated: dict, equipment: str, goal: str, completed: list[dict]) -> tuple[str, str]:
    if updated.get("duration_seconds") is not None:
        if updated["duration_seconds"] < DURATION_CAP:
            updated["duration_seconds"] = min(DURATION_CAP, updated["duration_seconds"] + DURATION_STEP)
            return "increase_duration", f"各组都完成了目标时长，下次 {_kg_duration(updated['duration_seconds'])}。"
        if updated["sets"] < MAX_SETS:
            updated["sets"] += 1
            return "add_set", "目标时长已到上限，增加 1 组。"
        return "hold", "时长和组数已到当前上限，维持原处方。"
    if equipment in LOAD_STEP:
        weights = _weights(completed) or []
        base = min(weights) if weights else float(updated.get("target_weight_kg") or 0)
        target = updated.get("target_weight_kg")
        if target is not None:
            base = max(base, float(target))
        updated["target_weight_kg"] = _round_kg(base + LOAD_STEP[equipment])
        return "increase_load", f"每一组都达到 {updated['reps_max']} 次，下次目标 {_kg(updated['target_weight_kg'])} kg，次数仍是 {updated['reps_min']}–{updated['reps_max']}。"
    cap = REP_CAP.get(goal, 15)
    if updated["reps_max"] < cap:
        updated["reps_min"] += 1
        updated["reps_max"] += 1
        return "increase_reps", f"各组都达到次数上限，下次 {updated['reps_min']}–{updated['reps_max']} 次。"
    if updated["sets"] < MAX_SETS:
        updated["sets"] += 1
        return "add_set", "次数已到该目标的上限，增加 1 组。"
    return "hold", "次数和组数已到当前目标的上限，维持原处方。"


def _reduce(updated: dict, equipment: str, goal: str, completed: list[dict]) -> tuple[str, str]:
    if updated.get("duration_seconds") is not None:
        if updated["duration_seconds"] > DURATION_FLOOR:
            updated["duration_seconds"] = max(DURATION_FLOOR, updated["duration_seconds"] - DURATION_STEP)
            return "reduce_duration", f"未达到目标时长，下次 {_kg_duration(updated['duration_seconds'])}。"
        if updated["sets"] > 2:
            updated["sets"] -= 1
            return "reduce_sets", "时长已较低，减少 1 组。"
        return "hold", "时长和组数已经较低，维持原处方。"
    if equipment in LOAD_STEP:
        weights = _weights(completed)
        anchor = updated.get("target_weight_kg")
        if anchor is None and weights:
            anchor = min(weights)
        if anchor is not None and float(anchor) > 0:
            new_weight = max(0, _round_kg(float(anchor) - LOAD_STEP[equipment]))
            if new_weight < float(anchor):
                updated["target_weight_kg"] = new_weight
                return "reduce_load", f"未达到本周目标，下次目标 {_kg(new_weight)} kg。"
    if _lower_reps(updated, goal):
        return "reduce_reps", f"未达到本周目标，次数调整为 {updated['reps_min']}–{updated['reps_max']}。"
    if updated["sets"] > 2:
        updated["sets"] -= 1
        return "reduce_sets", "次数已经较低，减少 1 组。"
    return "hold", "训练量已经较低，维持原处方。"


def _lower_reps(updated: dict, goal: str) -> bool:
    if updated.get("reps_min") is None or updated.get("reps_max") is None:
        return False
    floor = REP_FLOOR.get(goal, 6)
    reps_min, reps_max = updated["reps_min"], updated["reps_max"]
    if reps_min > floor:
        drop = min(2, reps_min - floor)
        updated["reps_min"] = reps_min - drop
        updated["reps_max"] = max(updated["reps_min"], reps_max - drop)
        return True
    if reps_max - reps_min >= 2:
        updated["reps_max"] = reps_max - 2
        return True
    if reps_max > reps_min:
        updated["reps_max"] = reps_min
        return True
    return False


def _restore_until_valid(data: dict, decisions: list[dict], profile: dict, docs: dict) -> tuple[PlanDraft, dict]:
    result: dict[str, Any] = {"valid": False, "errors": []}
    plan = PlanDraft.model_validate(data)
    for _ in range(len(decisions) + 1):
        plan = PlanDraft.model_validate(data)
        result = validate_plan(plan, profile, docs)
        if result["valid"] or not _revert_one(data, decisions, result["errors"]):
            break
    return plan, result


def _revert_one(data: dict, decisions: list[dict], errors: list[dict]) -> bool:
    for error in errors:
        decision = _pick_revert(data, decisions, error)
        if decision is None:
            continue
        original = decision["_original"]
        _set_exercise(data, decision["weekday"], decision["position"], copy.deepcopy(original))
        decision["action"] = "hold"
        decision["exercise_id"] = original["exercise_id"]
        decision["name"] = decision.get("_source_name", decision["name"])
        decision["after"] = _view(original)
        decision["reason"] = f"调整后未通过计划校验（{error['code']}），已恢复原处方。"
        if decision.get("replacement_candidates"):
            decision["reason"] += " 仍可从候选动作里另选。"
        return True
    return False


def _pick_revert(data: dict, decisions: list[dict], error: dict) -> dict | None:
    changed = [item for item in decisions if _current(data, item) != item["_original"]]
    if not changed:
        return None
    code = error.get("code")
    if code == "duration":
        same_day = [item for item in changed if item["weekday"] == error.get("weekday")]
        return max(same_day, key=lambda item: _cost(item["_original"], _current(data, item)), default=None)
    if error.get("exercise_id"):
        matched = [item for item in changed if _current(data, item)["exercise_id"] == error["exercise_id"]]
        return matched[0] if matched else None
    if code == "recovery":
        replaced = [item for item in changed if _current(data, item)["exercise_id"] != item["_original"]["exercise_id"]]
        return (replaced or changed)[0]
    return None


def _cost(before: dict, after: dict) -> tuple:
    return (
        after["sets"] - before["sets"],
        (after.get("duration_seconds") or 0) - (before.get("duration_seconds") or 0),
        (after.get("reps_max") or 0) - (before.get("reps_max") or 0),
        (after.get("target_weight_kg") or 0) - (before.get("target_weight_kg") or 0),
    )


def _current(data: dict, decision: dict) -> dict:
    for day in data["days"]:
        if day["weekday"] == decision["weekday"]:
            return day["exercises"][decision["position"]]
    raise KeyError(decision["weekday"])


def _set_exercise(data: dict, weekday: int, position: int, exercise: dict) -> None:
    for day in data["days"]:
        if day["weekday"] == weekday:
            day["exercises"][position] = exercise
            return


def _successful(sets: list[dict]) -> list[dict]:
    return [entry for entry in sets if entry.get("completed", True)]


def _hard(sets: list[dict]) -> bool:
    return any(entry.get("rpe") is not None and entry["rpe"] >= HIGH_RPE for entry in sets)


def _weights(sets: list[dict]) -> list[float] | None:
    values = [entry.get("weight_kg") for entry in sets]
    if any(value is None for value in values):
        return None
    return [float(value) for value in values]


def _copy(prescription: dict) -> dict:
    return {
        "exercise_id": prescription["exercise_id"],
        "sets": prescription["sets"],
        "reps_min": prescription.get("reps_min"),
        "reps_max": prescription.get("reps_max"),
        "duration_seconds": prescription.get("duration_seconds"),
        "rest_seconds": prescription.get("rest_seconds", 60),
        "rpe": prescription.get("rpe"),
        "target_weight_kg": prescription.get("target_weight_kg"),
        "notes": prescription.get("notes") or "",
    }


def _view(prescription: dict) -> dict:
    return {field: prescription.get(field) for field in VIEW_FIELDS}


def _public_candidate(row: dict) -> dict:
    return {key: row.get(key) for key in ("id", "name", "name_zh", "equipment", "level", "primary_muscles")}


def _round_kg(value: float) -> float:
    return round(value, 2)


def _kg(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _kg_duration(seconds: int) -> str:
    return f"{seconds} 秒"
