"""
工具协议与执行器。

契约（对应设计文档 2.3）：
- 参数用 Pydantic 模型定义，同时生成给模型的 JSON Schema 和服务端校验；
- 可信信息（user_id / session_id）只通过 ToolContext 由服务端注入，绝不出现在模型可见的参数中；
- 每个工具声明超时与是否有副作用；参数 JSON 解析失败或校验失败时直接返回错误，不会带着空参数执行；
- 执行异常统一转为失败结果反馈给模型，内部错误细节只写日志。
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from app.agent.types import ToolCall
from app.llm.tokens import truncate_to_tokens

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolContext:
    user_id: str
    session_id: str


class ToolError(Exception):
    """可以直接展示给模型的业务错误。"""


@dataclass
class ToolResult:
    success: bool
    data: Any = None
    error: str | None = None

    def to_content(self, max_tokens: int) -> str:
        payload = {"ok": True, "data": self.data} if self.success else {"ok": False, "error": self.error}
        return truncate_to_tokens(json.dumps(payload, ensure_ascii=False, default=str), max_tokens)


class EmptyParams(BaseModel):
    pass


def clean_schema(node: Any, in_properties: bool = False) -> Any:
    """精简 Pydantic 生成的 JSON Schema：去掉 title，把 Optional[X] 的 anyOf 折叠为 X。"""
    if isinstance(node, list):
        return [clean_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    cleaned = {}
    for key, value in node.items():
        if key == "title" and not in_properties and isinstance(value, str):
            continue
        cleaned[key] = clean_schema(value, in_properties=(key == "properties"))
    if in_properties:
        return cleaned
    any_of = cleaned.get("anyOf")
    if isinstance(any_of, list):
        non_null = [s for s in any_of if not (isinstance(s, dict) and s.get("type") == "null")]
        if len(non_null) == 1 and len(non_null) < len(any_of):
            merged = {k: v for k, v in cleaned.items() if k != "anyOf"}
            merged.update(non_null[0])
            cleaned = merged
    if "default" in cleaned and cleaned["default"] is None:
        cleaned.pop("default")
    return cleaned


class BaseTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    Params: ClassVar[type[BaseModel]] = EmptyParams
    side_effect: ClassVar[bool] = False
    timeout: ClassVar[float | None] = None

    def schema(self) -> dict[str, Any]:
        parameters = clean_schema(self.Params.model_json_schema())
        parameters.setdefault("properties", {})
        parameters["type"] = "object"
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }

    @abstractmethod
    async def run(self, params: Any, ctx: ToolContext) -> Any:
        """返回可 JSON 序列化的数据；业务错误抛出 ToolError。"""


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ())) or "参数"
        parts.append(f"{loc}: {err.get('msg')}")
    return "；".join(parts)


class ToolExecutor:
    def __init__(self, tools: list[BaseTool], default_timeout: float = 30.0):
        self._tools = {t.name: t for t in tools}
        self.default_timeout = default_timeout

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(False, error=f"未知工具：{call.name}，可用工具：{', '.join(self._tools)}")
        if call.parse_error:
            return ToolResult(False, error=f"参数不是合法的 JSON 对象（{call.parse_error}），请重新生成参数")
        try:
            params = tool.Params.model_validate(call.arguments)
        except ValidationError as exc:
            return ToolResult(False, error="参数校验失败：" + _format_validation_error(exc))

        timeout = tool.timeout or self.default_timeout
        try:
            data = await asyncio.wait_for(tool.run(params, ctx), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("工具 %s 超时（%.0fs）", call.name, timeout)
            return ToolResult(False, error=f"工具执行超时（{timeout:.0f} 秒）")
        except ToolError as exc:
            return ToolResult(False, error=str(exc))
        except Exception:
            logger.exception("工具 %s 执行失败 user=%s", call.name, ctx.user_id)
            return ToolResult(False, error="工具内部错误，请稍后重试或换一种方式完成任务")
        return ToolResult(True, data=data)
