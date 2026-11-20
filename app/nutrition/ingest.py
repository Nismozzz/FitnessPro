from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class MealChunk(BaseModel):
    """One structurally complete recipe document used for retrieval."""

    id: str
    name: str
    category: str = ""
    cuisine: str = ""
    methods: list[str] = Field(default_factory=list)
    flavors: list[str] = Field(default_factory=list)
    kcal: float | int | None = None
    desc: str = ""
    ingredients: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    nutrition: dict[str, Any] = Field(default_factory=dict)
    per100g: dict[str, Any] = Field(default_factory=dict)
    servingSize: dict[str, Any] = Field(default_factory=dict)
    mealType: list[str] = Field(default_factory=list)

    def search_text(self) -> str:
        nutrition = ", ".join(f"{k}: {v} g" for k, v in self.nutrition.items())
        per100 = ", ".join(f"{k}: {v} g" for k, v in self.per100g.items())
        serving = " ".join(str(v) for v in self.servingSize.values())
        return "\n".join([
            f"菜名：{self.name}", f"分类：{self.category}", f"菜系：{self.cuisine}",
            f"做法：{'、'.join(self.methods)}", f"口味：{'、'.join(self.flavors)}",
            f"热量：{self.kcal} kcal", f"餐次：{'、'.join(self.mealType)}",
            f"份量：{serving}", f"简介：{self.desc}",
            f"食材：{'；'.join(self.ingredients)}", f"营养素：{nutrition}",
            f"每100克：{per100}", f"步骤：{' '.join(self.steps)}",
        ])


def read_meals(path: Path) -> list[MealChunk]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("meals.json 必须是非空数组")
    chunks: list[MealChunk] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            raise ValueError(f"第 {index + 1} 条菜谱缺少 id 或 name")
        chunks.append(MealChunk.model_validate(item))
    if len({c.id for c in chunks}) != len(chunks):
        raise ValueError("meals.json 存在重复 id")
    return chunks


def catalog_hash(chunks: list[MealChunk]) -> str:
    payload = [c.model_dump() for c in chunks]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
