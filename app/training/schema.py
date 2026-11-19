from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.rag.schema import MuscleName
from app.users.profile import GoalName


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ExercisePrescription(StrictModel):
    exercise_id: str = Field(min_length=1, max_length=128)
    sets: int = Field(ge=1, le=10)
    reps_min: int | None = Field(None, ge=1, le=100)
    reps_max: int | None = Field(None, ge=1, le=100)
    duration_seconds: int | None = Field(None, ge=5, le=7200)
    rest_seconds: int = Field(60, ge=0, le=600)
    rpe: float | None = Field(None, ge=1, le=10)
    target_weight_kg: float | None = Field(None, ge=0, le=1000)
    notes: str = Field("", max_length=500)

    @model_validator(mode="after")
    def valid_measure(self):
        if self.duration_seconds is not None:
            if self.reps_min is not None or self.reps_max is not None:
                raise ValueError("计时动作与次数动作只能选择一种")
        elif self.reps_min is None or self.reps_max is None or self.reps_min > self.reps_max:
            raise ValueError("次数动作需要 reps_min <= reps_max，计时动作需要 duration_seconds")
        return self


class DayDraft(StrictModel):
    weekday: int = Field(ge=0, le=6, description="周一=0，周日=6")
    focus: str = Field(min_length=1, max_length=100)
    notes: str = Field("", max_length=500)
    warmup_minutes: int = Field(5, ge=0, le=30)
    cooldown_minutes: int = Field(5, ge=0, le=30)
    exercises: list[ExercisePrescription] = Field(min_length=1, max_length=15)

    @model_validator(mode="after")
    def unique_exercises(self):
        if len({e.exercise_id for e in self.exercises}) != len(self.exercises):
            raise ValueError("同一天不能重复添加相同动作，请合并组数")
        return self


class PlanDraft(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    goal: GoalName
    split_type: Literal["full_body", "upper_lower", "ppl", "custom"] = "full_body"
    week_start: date = Field(description="计划周的周一，YYYY-MM-DD")
    target_muscles: list[MuscleName] = Field(default_factory=list, max_length=17)
    days: list[DayDraft] = Field(min_length=1, max_length=7)

    @model_validator(mode="after")
    def valid_week(self):
        if self.week_start.weekday() != 0:
            raise ValueError("week_start 必须为周一")
        if len({d.weekday for d in self.days}) != len(self.days):
            raise ValueError("训练日不能重复")
        return self


class SetEntry(StrictModel):
    exercise_id: str = Field(min_length=1, max_length=128)
    set_no: int = Field(ge=1, le=100)
    reps: int | None = Field(None, ge=1, le=200)
    duration_seconds: int | None = Field(None, ge=1, le=86400)
    weight_kg: float | None = Field(None, ge=0, le=1000)
    rpe: float | None = Field(None, ge=1, le=10)
    completed: bool = True

    @model_validator(mode="after")
    def measurement(self):
        if self.completed and (self.reps is None) == (self.duration_seconds is None):
            raise ValueError("已完成的组必须填写次数或时长，且只能填写一种")
        return self


class LogCreate(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=100, description="同一次记录重试复用同一键；不同训练使用新键")
    plan_day_id: str | None = None
    performed_at: datetime = Field(description="包含时区偏移的训练时间，例如 2026-11-16T19:00:00+08:00")
    duration_min: int = Field(ge=1, le=720)
    rpe: float | None = Field(None, ge=1, le=10)
    notes: str = Field("", max_length=1000)
    sets: list[SetEntry] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_log(self):
        if self.performed_at.tzinfo is None:
            raise ValueError("performed_at 必须包含时区")
        keys = {(s.exercise_id, s.set_no) for s in self.sets}
        if len(keys) != len(self.sets):
            raise ValueError("同一动作组号不能重复")
        return self


class Replacement(StrictModel):
    weekday: int = Field(ge=0, le=6)
    position: int = Field(ge=0, le=14, description="动作序号，从 0 开始")
    exercise: ExercisePrescription
