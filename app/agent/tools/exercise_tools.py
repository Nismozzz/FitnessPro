from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agent.tools.base import BaseTool, ToolContext, ToolError
from app.config import settings
from app.rag.retriever import KnowledgeBaseNotReady, get_retriever
from app.rag.schema import ExerciseFilter


class SearchExercisesParams(ExerciseFilter):
    query: str = Field(
        "",
        max_length=200,
        description=(
            "自然语言检索词，描述想练的部位、动作特点或动作名称，"
            "如“练上胸的动作”“对膝盖压力小的腿部训练”“卧推”。只按条件筛选时可留空。"
        ),
    )
    top_k: int = Field(6, ge=1, le=15, description="返回数量，默认 6")


class SearchExercisesTool(BaseTool):
    name = "search_exercises"
    description = (
        "检索健身动作知识库（约 870 个动作，含中英文名称、目标肌群、器械、难度、动作类型）。"
        "语义检索与关键词检索结合，并可按肌群、器械、难度等条件精确筛选。推荐任何动作前都应先调用。"
    )
    Params = SearchExercisesParams

    async def run(self, params: SearchExercisesParams, ctx: ToolContext) -> Any:
        try:
            retriever = get_retriever()
        except KnowledgeBaseNotReady as exc:
            raise ToolError(str(exc)) from exc
        result = await retriever.search(params.query, params.to_filter(), params.top_k)
        return {
            "matched_by_filters": result.total_candidates,
            "retrieval_mode": result.mode,
            "warnings": result.warnings,
            "exercises": [h.doc.summary() for h in result.hits],
        }


class ExerciseDetailParams(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5, description="动作 id 列表（来自 search_exercises 结果）")


class GetExerciseDetailTool(BaseTool):
    name = "get_exercise_detail"
    description = "按 id 获取动作详情：完整动作步骤、次要肌群、示意图链接。"
    Params = ExerciseDetailParams

    async def run(self, params: ExerciseDetailParams, ctx: ToolContext) -> Any:
        try:
            retriever = get_retriever()
        except KnowledgeBaseNotReady as exc:
            raise ToolError(str(exc)) from exc
        found, missing = [], []
        for doc_id in params.ids:
            doc = retriever.get(doc_id)
            if doc is None:
                missing.append(doc_id)
            else:
                found.append(doc.detail(settings.data.image_base_url))
        return {"exercises": found, "not_found": missing}
