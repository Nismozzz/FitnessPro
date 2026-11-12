"""
LLM 调用封装：基于 OpenAI SDK，兼容 DeepSeek / SiliconFlow / 通义等 OpenAI 兼容接口。

相比 CookHero 的 LangChain 封装，这里直接使用 SDK：工具调用、流式分片、
reasoning_content 等字段都能原样拿到，Agent 循环的行为更透明。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any, Literal

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from app.config import LLMProfile, settings

logger = logging.getLogger(__name__)

Tier = Literal["fast", "normal"]


class LLMClient:
    def __init__(self, profile: LLMProfile, tier: str):
        if not profile.api_key:
            logger.warning("LLM[%s] 未配置 API Key（环境变量 %s）", tier, profile.api_key_env)
        self.profile = profile
        self.tier = tier
        self._client = AsyncOpenAI(
            api_key=profile.api_key or "missing",
            base_url=profile.base_url,
            timeout=profile.timeout,
            max_retries=2,
        )

    @property
    def model(self) -> str:
        return self.profile.model

    def _params(self, overrides: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.profile.model,
            "temperature": self.profile.temperature,
            "max_tokens": self.profile.max_tokens,
        }
        if self.profile.extra_body:
            params["extra_body"] = self.profile.extra_body
        params.update({k: v for k, v in overrides.items() if v is not None})
        return params

    async def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        json_mode: bool = False,
        **overrides: Any,
    ) -> ChatCompletion:
        params = self._params(overrides)
        if tools:
            params["tools"] = tools
        if json_mode:
            params["response_format"] = {"type": "json_object"}
        start = time.perf_counter()
        response = await self._client.chat.completions.create(messages=messages, **params)
        usage = response.usage
        logger.info(
            "llm tier=%s model=%s prompt=%s completion=%s ms=%d",
            self.tier,
            params["model"],
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            (time.perf_counter() - start) * 1000,
        )
        return response

    async def complete_text(self, messages: list[dict], **overrides: Any) -> str:
        response = await self.complete(messages, **overrides)
        return response.choices[0].message.content or ""

    async def complete_json(self, messages: list[dict], **overrides: Any) -> Any:
        response = await self.complete(messages, json_mode=True, **overrides)
        return parse_json_loose(response.choices[0].message.content or "")

    async def stream(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        **overrides: Any,
    ) -> AsyncIterator[ChatCompletionChunk]:
        params = self._params(overrides)
        if tools:
            params["tools"] = tools
        stream = await self._client.chat.completions.create(
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            **params,
        )
        async for chunk in stream:
            yield chunk


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_loose(text: str) -> Any:
    """解析模型输出的 JSON，容忍 ```json 代码块和前后多余文字。"""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


@lru_cache
def get_llm(tier: Tier = "normal") -> LLMClient:
    profile = getattr(settings.llm, tier)
    return LLMClient(profile, tier)
