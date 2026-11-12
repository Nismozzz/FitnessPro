"""
动作知识库的领域模型：枚举值（含中文标签与同义词）、动作文档、结构化筛选条件。

free-exercise-db 的字段都是固定枚举，因此“按肌群/器械/难度筛选”走精确过滤，
语义检索只在过滤后的候选集合内排序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 每个枚举值对应 (显示标签, 同义词...)，同义词用于中文输入归一化与 BM25 召回
MUSCLES: dict[str, tuple[str, ...]] = {
    "abdominals": ("腹肌", "腹部", "核心", "腹直肌"),
    "abductors": ("髋外展肌", "臀中肌", "髋外侧"),
    "adductors": ("髋内收肌", "大腿内侧", "内收肌"),
    "biceps": ("肱二头肌", "二头肌", "二头"),
    "calves": ("小腿", "小腿肌", "腓肠肌", "比目鱼肌"),
    "chest": ("胸部", "胸肌", "胸大肌", "胸"),
    "forearms": ("前臂", "小臂", "握力"),
    "glutes": ("臀部", "臀肌", "臀大肌", "臀"),
    "hamstrings": ("腘绳肌", "大腿后侧", "股二头肌"),
    "lats": ("背阔肌", "背部", "背"),
    "lower back": ("下背部", "下背", "竖脊肌", "腰部"),
    "middle back": ("中背部", "中背", "菱形肌"),
    "neck": ("颈部", "脖子"),
    "quadriceps": ("股四头肌", "大腿前侧", "股四"),
    "shoulders": ("肩部", "三角肌", "肩膀", "肩"),
    "traps": ("斜方肌", "斜方"),
    "triceps": ("肱三头肌", "三头肌", "三头"),
}

EQUIPMENT: dict[str, tuple[str, ...]] = {
    "body only": ("徒手", "自重", "无器械"),
    "barbell": ("杠铃",),
    "dumbbell": ("哑铃",),
    "kettlebells": ("壶铃",),
    "cable": ("绳索", "龙门架", "拉力器"),
    "machine": ("固定器械", "器械"),
    "bands": ("弹力带", "阻力带"),
    "medicine ball": ("药球",),
    "exercise ball": ("瑜伽球", "健身球", "瑞士球"),
    "foam roll": ("泡沫轴",),
    "e-z curl bar": ("曲杆", "EZ杆", "ez杆"),
    "other": ("其他器械", "其他"),
}

LEVELS: dict[str, tuple[str, ...]] = {
    "beginner": ("初级", "新手", "入门"),
    "intermediate": ("中级", "进阶"),
    "expert": ("高级", "专家"),
}

CATEGORIES: dict[str, tuple[str, ...]] = {
    "strength": ("力量训练", "力量", "无氧"),
    "stretching": ("拉伸", "柔韧", "放松"),
    "plyometrics": ("增强式训练", "爆发力", "跳跃"),
    "powerlifting": ("力量举",),
    "olympic weightlifting": ("奥林匹克举重", "举重"),
    "strongman": ("大力士",),
    "cardio": ("有氧", "心肺"),
}

FORCES: dict[str, tuple[str, ...]] = {
    "push": ("推",),
    "pull": ("拉",),
    "static": ("静力", "静态"),
}

MECHANICS: dict[str, tuple[str, ...]] = {
    "compound": ("复合动作", "多关节"),
    "isolation": ("孤立动作", "单关节"),
}

MuscleName = Literal[
    "abdominals", "abductors", "adductors", "biceps", "calves", "chest", "forearms",
    "glutes", "hamstrings", "lats", "lower back", "middle back", "neck", "quadriceps",
    "shoulders", "traps", "triceps",
]
EquipmentName = Literal[
    "body only", "barbell", "dumbbell", "kettlebells", "cable", "machine", "bands",
    "medicine ball", "exercise ball", "foam roll", "e-z curl bar", "other",
]
LevelName = Literal["beginner", "intermediate", "expert"]
CategoryName = Literal[
    "strength", "stretching", "plyometrics", "powerlifting", "olympic weightlifting",
    "strongman", "cardio",
]
ForceName = Literal["push", "pull", "static"]
MechanicName = Literal["compound", "isolation"]

for _lit, _table in (
    (MuscleName, MUSCLES),
    (EquipmentName, EQUIPMENT),
    (LevelName, LEVELS),
    (CategoryName, CATEGORIES),
    (ForceName, FORCES),
    (MechanicName, MECHANICS),
):
    assert set(get_args(_lit)) == set(_table), f"枚举不一致: {_lit}"

LEVEL_ORDER = {"beginner": 0, "intermediate": 1, "expert": 2}


def label(table: dict[str, tuple[str, ...]], key: str | None) -> str:
    if not key:
        return "未标注"
    return table.get(key, (key,))[0]


def normalize(table: dict[str, tuple[str, ...]], value: str) -> str | None:
    """把英文枚举或中文同义词归一化为枚举值。"""
    raw = value.strip()
    lowered = raw.lower()
    if lowered in table:
        return lowered
    for key, synonyms in table.items():
        if raw in synonyms or lowered in (s.lower() for s in synonyms):
            return key
    return None


def enum_desc(table: dict[str, tuple[str, ...]]) -> str:
    return "，".join(f"{k}（{v[0]}）" for k, v in table.items())


def _normalize_list(value: Any, table: dict[str, tuple[str, ...]]) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if isinstance(value, list):
        # 无法识别的值原样保留，交给 Literal 校验报错，让模型看到可选值
        return [normalize(table, v) or v if isinstance(v, str) else v for v in value]
    return value


class ExerciseFilter(BaseModel):
    """结构化筛选条件。同一字段内多个值为“或”，不同字段之间为“且”。"""

    model_config = ConfigDict(extra="ignore")

    primary_muscles: list[MuscleName] | None = Field(
        None, max_length=6, description="目标主要肌群，满足任意一个即可。可选值：" + enum_desc(MUSCLES)
    )
    equipment: list[EquipmentName] | None = Field(
        None,
        max_length=12,
        description="允许使用的器械，满足任意一个即可；徒手为 body only。可选值：" + enum_desc(EQUIPMENT),
    )
    levels: list[LevelName] | None = Field(
        None, max_length=3, description="难度。可选值：" + enum_desc(LEVELS)
    )
    categories: list[CategoryName] | None = Field(
        None,
        max_length=7,
        description="动作类别。一般力量训练用 strength，拉伸放松用 stretching。可选值：" + enum_desc(CATEGORIES),
    )
    force: list[ForceName] | None = Field(
        None, max_length=3, description="发力方式。可选值：" + enum_desc(FORCES)
    )
    mechanic: MechanicName | None = Field(
        None, description="动作类型。可选值：" + enum_desc(MECHANICS)
    )
    exclude_ids: list[str] | None = Field(
        None, max_length=50, description="需要排除的动作 id，例如已经推荐过的动作"
    )

    @field_validator("primary_muscles", mode="before")
    @classmethod
    def _n_muscles(cls, v: Any) -> Any:
        return _normalize_list(v, MUSCLES)

    @field_validator("equipment", mode="before")
    @classmethod
    def _n_equipment(cls, v: Any) -> Any:
        return _normalize_list(v, EQUIPMENT)

    @field_validator("levels", mode="before")
    @classmethod
    def _n_levels(cls, v: Any) -> Any:
        return _normalize_list(v, LEVELS)

    @field_validator("categories", mode="before")
    @classmethod
    def _n_categories(cls, v: Any) -> Any:
        return _normalize_list(v, CATEGORIES)

    @field_validator("force", mode="before")
    @classmethod
    def _n_force(cls, v: Any) -> Any:
        return _normalize_list(v, FORCES)

    @field_validator("mechanic", mode="before")
    @classmethod
    def _n_mechanic(cls, v: Any) -> Any:
        if isinstance(v, str):
            return normalize(MECHANICS, v) or v
        return v

    def to_filter(self) -> "ExerciseFilter":
        """子类（工具参数）转换为纯筛选条件。"""
        return ExerciseFilter.model_validate(
            self.model_dump(include=set(ExerciseFilter.model_fields))
        )

    def matches(self, doc: "ExerciseDoc") -> bool:
        if self.primary_muscles and not set(self.primary_muscles) & set(doc.primary_muscles):
            return False
        # 数据中 equipment 可能为空：指定器械条件时，未标注器械的动作不返回
        if self.equipment and doc.equipment not in self.equipment:
            return False
        if self.levels and doc.level not in self.levels:
            return False
        if self.categories and doc.category not in self.categories:
            return False
        if self.force and doc.force not in self.force:
            return False
        if self.mechanic and doc.mechanic != self.mechanic:
            return False
        if self.exclude_ids and doc.id in self.exclude_ids:
            return False
        return True


@dataclass
class ExerciseDoc:
    id: str
    name: str
    level: str
    category: str
    primary_muscles: list[str]
    secondary_muscles: list[str]
    instructions: list[str]
    force: str | None = None
    mechanic: str | None = None
    equipment: str | None = None
    images: list[str] = field(default_factory=list)
    name_zh: str | None = None
    aliases_zh: list[str] = field(default_factory=list)
    instructions_zh: list[str] = field(default_factory=list)

    @classmethod
    def from_model(cls, m: Any) -> "ExerciseDoc":
        return cls(
            id=m.id,
            name=m.name,
            level=m.level,
            category=m.category,
            primary_muscles=list(m.primary_muscles or []),
            secondary_muscles=list(m.secondary_muscles or []),
            instructions=list(m.instructions or []),
            force=m.force,
            mechanic=m.mechanic,
            equipment=m.equipment,
            images=list(m.images or []),
            name_zh=m.name_zh,
            aliases_zh=list(m.aliases_zh or []),
            instructions_zh=list(m.instructions_zh or []),
        )

    @property
    def display_name(self) -> str:
        return f"{self.name_zh}（{self.name}）" if self.name_zh else self.name

    def search_text(self, max_instruction_chars: int = 600) -> str:
        """用于 Embedding / BM25 / Rerank 的检索文本：中英双语 + 枚举同义词。"""

        def muscle(m: str) -> str:
            syns = MUSCLES.get(m)
            return f"{'/'.join(syns)}（{m}）" if syns else m

        lines = [f"动作：{self.display_name}"]
        if self.aliases_zh:
            lines.append("别名：" + "、".join(self.aliases_zh))
        lines.append(
            f"类别：{label(CATEGORIES, self.category)}；难度：{label(LEVELS, self.level)}；"
            f"器械：{label(EQUIPMENT, self.equipment)}（{self.equipment or '-'}）；"
            f"发力：{label(FORCES, self.force)}；类型：{label(MECHANICS, self.mechanic)}"
        )
        lines.append("主要肌群：" + "、".join(muscle(m) for m in self.primary_muscles))
        if self.secondary_muscles:
            lines.append("次要肌群：" + "、".join(label(MUSCLES, m) for m in self.secondary_muscles))
        steps = " ".join(self.instructions_zh or self.instructions)
        if steps:
            lines.append("动作要点：" + steps[:max_instruction_chars])
        return "\n".join(lines)

    def summary(self) -> dict[str, Any]:
        """给 LLM 的精简信息（检索结果列表）。"""
        return {
            "id": self.id,
            "name_zh": self.name_zh,
            "name": self.name,
            "primary_muscles": [label(MUSCLES, m) for m in self.primary_muscles],
            "secondary_muscles": [label(MUSCLES, m) for m in self.secondary_muscles],
            "equipment": label(EQUIPMENT, self.equipment),
            "level": label(LEVELS, self.level),
            "mechanic": label(MECHANICS, self.mechanic),
            "category": label(CATEGORIES, self.category),
        }

    def detail(self, image_base_url: str) -> dict[str, Any]:
        data = self.summary()
        data["force"] = label(FORCES, self.force)
        data["aliases_zh"] = self.aliases_zh
        data["instructions"] = self.instructions_zh or self.instructions
        data["instructions_language"] = "zh" if self.instructions_zh else "en"
        data["images"] = [image_base_url + p for p in self.images]
        return data
