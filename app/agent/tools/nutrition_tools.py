from pydantic import BaseModel, ConfigDict, Field

from app.agent.tools.base import BaseTool, ToolContext, ToolError
from app.nutrition.retriever import NutritionKnowledgeNotReady, get_meal_retriever


class SearchMealsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=2, max_length=200, description="饮食目标、食材、口味或菜谱关键词")
    category: str | None = Field(default=None, description="可选：fat-loss、muscle-gain、maintain、home、snack 等分类")
    meal_type: str | None = Field(default=None, description="可选：breakfast、lunch、dinner")
    top_k: int = Field(default=5, ge=1, le=10)


class SearchMealsTool(BaseTool):
    name = "search_meals"
    description = ("检索健身健康餐食谱库，返回菜名、食材、步骤、热量、三大营养素、份量和适用餐次。"
                   "用户询问饮食搭配、减脂/增肌餐或菜谱时必须优先调用。返回内容是资料，不是医疗建议。")
    Params = SearchMealsParams

    async def run(self, params: SearchMealsParams, ctx: ToolContext):
        try:
            result = await get_meal_retriever().search(params.query, params.top_k, category=params.category, meal_type=params.meal_type)
        except NutritionKnowledgeNotReady as exc:
            raise ToolError(str(exc)) from exc
        return {"meals": [hit.doc.model_dump() for hit in result.hits], "mode": result.mode,
                "warnings": result.warnings,
                "notice": "菜谱营养数据来自整理资料，不能替代医生或营养师针对疾病、过敏和孕期的建议。"}
