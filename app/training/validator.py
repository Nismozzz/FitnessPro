"""Deterministic validation. The caller supplies profile and exercise snapshots."""
from collections import defaultdict
from typing import Any

from app.rag.schema import LEVEL_ORDER
from app.training.equipment import effective_equipment
from app.training.schema import PlanDraft

GROUPS = {
    "abdominals": "核心", "abductors": "臀部", "adductors": "大腿", "biceps": "肱二头肌",
    "calves": "小腿", "chest": "胸部", "forearms": "前臂", "glutes": "臀部",
    "hamstrings": "大腿后侧", "lats": "背部", "lower back": "下背部", "middle back": "背部",
    "neck": "颈部", "quadriceps": "大腿前侧", "shoulders": "肩部", "traps": "斜方肌", "triceps": "肱三头肌",
}
INJURY_RISKS = [
    (("膝", "knee"), ("squat", "jump", "lunge", "深蹲", "跳", "弓步")),
    (("肩", "shoulder"), ("press", "dip", "推举", "臂屈伸")),
    (("腰", "下背", "lumbar", "back pain"), ("deadlift", "squat", "硬拉", "深蹲")),
    (("腕", "wrist"), ("push-up", "pushup", "press", "俯卧撑", "推举")),
    (("踝", "ankle"), ("jump", "run", "跳", "跑")),
]


def muscle_credits(doc: dict) -> dict[str, float]:
    credits = {GROUPS.get(m, m): 1.0 for m in doc.get("primary_muscles", [])}
    for muscle in doc.get("secondary_muscles", []):
        group = GROUPS.get(muscle, muscle)
        credits.setdefault(group, 0.5)
    return credits


def validate_plan(plan: PlanDraft, profile: dict[str, Any], exercises: dict[str, dict]) -> dict:
    errors, warnings = [], []
    def issue(target, code, message, **extra):
        target.append({"code": code, "message": message, **extra})
    available = effective_equipment(profile)
    if available is None:
        issue(errors, "missing_equipment", "请先确认训练场景或可用器械")
    for key, label in (("experience_level", "训练水平"), ("training_days_per_week", "每周天数"), ("session_minutes", "单次时长"), ("goal", "训练目标")):
        if profile.get(key) is None:
            issue(errors, "missing_profile", f"请先确认{label}", field=key)
    if profile.get("goal") and plan.goal != profile["goal"]:
        issue(warnings, "goal_mismatch", "计划目标与当前档案不同，请确认是否需要更新档案")
    if profile.get("training_days_per_week") and len(plan.days) > profile["training_days_per_week"]:
        issue(errors, "training_days", "训练天数超过用户可用天数")
    level = profile.get("experience_level") or "beginner"
    volumes = defaultdict(float)
    schedule = defaultdict(set)
    durations = {}
    injuries = " ".join(profile.get("injuries") or []).lower()
    for day in plan.days:
        seconds = (day.warmup_minutes + day.cooldown_minutes) * 60
        if not day.warmup_minutes or not day.cooldown_minutes:
            issue(warnings, "warmup_cooldown", "建议安排热身和放松", weekday=day.weekday)
        for prescription in day.exercises:
            doc = exercises.get(prescription.exercise_id)
            if doc is None:
                issue(errors, "unknown_exercise", "动作不存在于知识库", exercise_id=prescription.exercise_id)
                continue
            eid = prescription.exercise_id
            if available is not None and doc.get("equipment") not in available:
                issue(errors, "equipment", "动作器械不可用或未标注，请替换动作", exercise_id=eid)
            if LEVEL_ORDER.get(doc.get("level"), 99) > LEVEL_ORDER.get(level, 0) + 1:
                issue(errors, "difficulty", "动作难度超过允许水平", exercise_id=eid)
            elif level == "beginner" and doc.get("level") == "intermediate":
                issue(warnings, "difficulty", "新手优先选择初级动作，此动作为中级", exercise_id=eid)
            if doc.get("category") not in {"stretching", "cardio"}:
                for muscle in doc.get("primary_muscles", []):
                    schedule[GROUPS.get(muscle, muscle)].add(day.weekday)
                for group, factor in muscle_credits(doc).items():
                    volumes[group] += prescription.sets * factor
            work = prescription.duration_seconds or (prescription.reps_max or 10) * 3
            seconds += prescription.sets * work + max(0, prescription.sets - 1) * prescription.rest_seconds + 60
            if prescription.reps_min is not None:
                lo, hi = prescription.reps_min, prescription.reps_max
                matched = (3 <= lo <= hi <= 6) if plan.goal == "strength" else (6 <= lo <= hi <= 12) if plan.goal == "muscle_gain" else (lo >= 12) if plan.goal == "endurance" else True
                if not matched:
                    issue(warnings, "rep_range", "次数范围与目标的建议区间不同", exercise_id=eid)
            name = f"{doc.get('name', '')} {doc.get('name_zh', '')}".lower()
            if any(any(k in injuries for k in keys) and any(k in name for k in risks) for keys, risks in INJURY_RISKS):
                issue(warnings, "injury_risk", "该动作可能涉及已知伤病部位，建议替换，并咨询医生或康复师", exercise_id=eid)
        durations[day.weekday] = round(seconds / 60, 1)
        if profile.get("session_minutes") and seconds > profile["session_minutes"] * 60:
            issue(errors, "duration", "估算时长超过单次可用时间（含热身、放松及动作切换）", weekday=day.weekday, estimated_minutes=durations[day.weekday])
    for group, days in schedule.items():
        ordered = sorted(days)
        if len(ordered) > 1:
            gaps = [b - a for a, b in zip(ordered, ordered[1:] + [ordered[0] + 7])]
            if min(gaps) < 2:
                issue(errors, "recovery", "同一主要肌群训练日期应至少间隔两天，包含周末跨周", muscle=group)
    low, high = {"beginner": (8, 12), "intermediate": (10, 16), "expert": (12, 20)}[level]
    targets = {GROUPS[m] for m in plan.target_muscles} if plan.target_muscles else set(volumes)
    for group in sorted(targets | set(volumes)):
        total = volumes[group]
        if total < low or total > high:
            issue(warnings, "weekly_volume", f"{group}每周等效 {total:g} 组，当前水平参考范围 {low}–{high} 组", muscle=group, sets=total)
    if injuries:
        issue(warnings, "injury_notice", "已考虑已知身体限制；关键词筛查不能替代医生或康复师评估")
    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "estimated_minutes": durations, "weekly_sets": dict(volumes)}
