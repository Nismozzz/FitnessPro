"""
用户画像：结构化、用户可编辑的长期偏好（对应设计文档“先完成显式可编辑的用户偏好”）。

API 与 Agent 工具共用 ProfileData 做参数校验；user_id 永远来自服务端认证上下文。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.database.models import UserProfile
from app.database.session import SessionLocal
from app.rag.schema import EQUIPMENT, LEVELS, EquipmentName, LevelName, enum_desc, label, normalize
from app.utils.time import utcnow

GOALS: dict[str, tuple[str, ...]] = {
    "muscle_gain": ("增肌",),
    "fat_loss": ("减脂", "减肥"),
    "strength": ("提升力量", "力量"),
    "endurance": ("提升耐力与体能", "耐力", "体能"),
    "general_fitness": ("保持健康", "健康"),
    "rehab": ("康复与体态改善", "康复", "体态"),
}
GENDERS = {"male": ("男",), "female": ("女",), "other": ("其他",)}

GoalName = Literal["muscle_gain", "fat_loss", "strength", "endurance", "general_fitness", "rehab"]

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class ProfileData(BaseModel):
    """所有字段可选；更新时只写入非空字段。"""

    model_config = ConfigDict(extra="ignore")

    gender: Literal["male", "female", "other"] | None = Field(None, description="性别")
    age: int | None = Field(None, ge=10, le=100, description="年龄")
    height_cm: float | None = Field(None, ge=100, le=250, description="身高（厘米）")
    weight_kg: float | None = Field(None, ge=25, le=300, description="体重（公斤）")
    goal: GoalName | None = Field(None, description="主要训练目标。可选值：" + enum_desc(GOALS))
    experience_level: LevelName | None = Field(
        None, description="训练水平。可选值：" + enum_desc(LEVELS)
    )
    training_days_per_week: int | None = Field(None, ge=1, le=7, description="每周可训练天数")
    session_minutes: int | None = Field(None, ge=15, le=240, description="每次训练可用时长（分钟）")
    equipment: list[EquipmentName] | None = Field(
        None,
        max_length=12,
        description="可用器械（整体替换原列表）。可选值：" + enum_desc(EQUIPMENT),
    )
    injuries: list[ShortText] | None = Field(
        None, max_length=10, description="伤病或身体限制（整体替换原列表），如“左膝半月板旧伤”"
    )
    notes: str | None = Field(None, max_length=500, description="其他备注")

    @field_validator("goal", mode="before")
    @classmethod
    def _n_goal(cls, v: Any) -> Any:
        return normalize(GOALS, v) or v if isinstance(v, str) else v

    @field_validator("experience_level", mode="before")
    @classmethod
    def _n_level(cls, v: Any) -> Any:
        return normalize(LEVELS, v) or v if isinstance(v, str) else v

    @field_validator("gender", mode="before")
    @classmethod
    def _n_gender(cls, v: Any) -> Any:
        return normalize(GENDERS, v) or v if isinstance(v, str) else v

    @field_validator("equipment", mode="before")
    @classmethod
    def _n_equipment(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [normalize(EQUIPMENT, x) or x if isinstance(x, str) else x for x in v]
        return v


PROFILE_FIELDS = list(ProfileData.model_fields)


async def get_profile(user_id: str) -> dict[str, Any]:
    async with SessionLocal() as db:
        row = await db.get(UserProfile, user_id)
    if row is None:
        return {f: None for f in PROFILE_FIELDS}
    return {f: getattr(row, f) for f in PROFILE_FIELDS}


async def update_profile(user_id: str, data: ProfileData) -> dict[str, Any]:
    changes = data.model_dump(exclude_none=True)
    async with SessionLocal() as db:
        row = await db.get(UserProfile, user_id)
        if row is None:
            row = UserProfile(user_id=user_id)
            db.add(row)
        for key, value in changes.items():
            setattr(row, key, value)
        row.updated_at = utcnow()
        await db.commit()
    return await get_profile(user_id)


def render_profile(profile: dict[str, Any]) -> str:
    lines = []
    if profile.get("gender"):
        lines.append(f"- 性别：{label(GENDERS, profile['gender'])}")
    if profile.get("age"):
        lines.append(f"- 年龄：{profile['age']}")
    if profile.get("height_cm"):
        lines.append(f"- 身高：{profile['height_cm']} cm")
    if profile.get("weight_kg"):
        lines.append(f"- 体重：{profile['weight_kg']} kg")
    if profile.get("goal"):
        lines.append(f"- 目标：{label(GOALS, profile['goal'])}")
    if profile.get("experience_level"):
        lines.append(f"- 训练水平：{label(LEVELS, profile['experience_level'])}")
    if profile.get("training_days_per_week"):
        lines.append(f"- 每周可训练：{profile['training_days_per_week']} 天")
    if profile.get("session_minutes"):
        lines.append(f"- 每次时长：{profile['session_minutes']} 分钟")
    if profile.get("equipment"):
        names = "、".join(f"{label(EQUIPMENT, e)}({e})" for e in profile["equipment"])
        lines.append(f"- 可用器械：{names}")
    if profile.get("injuries"):
        lines.append(f"- 伤病/限制：{'；'.join(profile['injuries'])}")
    if profile.get("notes"):
        lines.append(f"- 备注：{profile['notes']}")
    if not lines:
        return "（用户尚未填写画像。需要时可询问目标、训练水平、每周可训练天数、器械条件和伤病情况。）"
    return "\n".join(lines)
