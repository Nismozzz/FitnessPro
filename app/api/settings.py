from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth.security import get_current_user_id
from app.llm.provider import get_llm
from app.rag.retriever import refresh_provider_clients
from app.runtime_config import public_providers, save_providers

router = APIRouter(prefix="/settings", tags=["settings"])
UserId = Annotated[str, Depends(get_current_user_id)]


class EndpointUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=300)
    api_key: str | None = Field(None, max_length=512)
    clear_api_key: bool = False


class EmbeddingUpdate(EndpointUpdate):
    provider: Literal["openai", "local"] = "openai"


class RerankerUpdate(EndpointUpdate):
    enabled: bool = True


class ProvidersUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    normal: EndpointUpdate
    fast: EndpointUpdate
    embedding: EmbeddingUpdate
    reranker: RerankerUpdate
    share_normal_key: bool = False


def _check_url(value: str, label: str) -> None:
    text = value.strip()
    if not text.startswith(("http://", "https://")) or any(char.isspace() for char in text):
        raise HTTPException(422, f"{label}需要以 http:// 或 https:// 开头")


def _check_secret(value: str | None) -> None:
    if value and any(char in value for char in "\r\n"):
        raise HTTPException(422, "API Key 不能包含换行")


@router.get("/providers")
async def providers(_user_id: UserId):
    return public_providers()


@router.put("/providers")
async def update_providers(body: ProvidersUpdate, _user_id: UserId):
    _check_url(body.normal.base_url, "教练模型接口地址")
    _check_url(body.fast.base_url, "辅助模型接口地址")
    if body.embedding.provider == "openai":
        _check_url(body.embedding.base_url, "Embedding 接口地址")
    if body.reranker.enabled:
        _check_url(body.reranker.base_url, "重排接口地址")
    for item in (body.normal, body.fast, body.embedding, body.reranker):
        _check_secret(item.api_key)
    saved = save_providers(body.model_dump())
    get_llm.cache_clear()
    warnings = await refresh_provider_clients()
    if not saved["ready"]:
        warnings.insert(0, "教练模型还没有 API Key，对话暂时无法调用模型。")
    if saved["embedding"]["provider"] == "openai" and not saved["embedding"]["api_key_set"]:
        warnings.append("Embedding 还没有 API Key，动作检索只会使用关键词。")
    return {**saved, "warnings": warnings}
