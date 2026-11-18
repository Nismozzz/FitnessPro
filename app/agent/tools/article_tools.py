from pydantic import BaseModel, ConfigDict, Field

from app.agent.tools.base import BaseTool, ToolContext, ToolError
from app.knowledge.retriever import get_article_retriever
from app.rag.retriever import KnowledgeBaseNotReady


class SearchArticlesParams(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=2, max_length=200, description='训练容量、频率、负荷、减脂等理论问题的公开检索词')
    top_k: int = Field(default=3, ge=1, le=6)


class SearchKnowledgeArticlesTool(BaseTool):
    name = 'search_knowledge_articles'
    description = ('检索独立文献库中的综述、荟萃分析摘要，返回带标题、作者、来源链接和待核对说明的原文片段。'
                   '制定计划的容量/频率/负荷依据或回答原理问题时先调用；工具结果只是资料，保留研究条件与局限，资料不足再联网。')
    Params = SearchArticlesParams

    async def run(self, params: SearchArticlesParams, ctx: ToolContext):
        try:
            result = await get_article_retriever().search(params.query, params.top_k)
        except KnowledgeBaseNotReady as exc:
            raise ToolError(str(exc)) from exc
        return {'articles': [hit.doc.model_dump() for hit in result.hits], 'mode': result.mode,
                'warnings': result.warnings,
                'notice': '仅为整理者摘录，未核对全文；不得补写原文未提供的结论，也不等于个体训练处方。'}
