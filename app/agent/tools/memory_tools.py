from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agent.tools.base import BaseTool, ToolContext, ToolError
from app.memory.service import (
    ACTIVE,
    KIND_LABELS,
    MemoryKind,
    MemoryOpError,
    MemoryScope,
    memory_service,
    memory_to_dict,
)


class SaveMemoryParams(BaseModel):
    kind: MemoryKind = Field(
        description="类型：" + "，".join(f"{k}（{v}）" for k, v in KIND_LABELS.items())
    )
    content: str = Field(min_length=2, max_length=200, description="简洁的第三人称事实陈述，如“用户左膝有旧伤，避免深蹲到底”")
    scope: MemoryScope = Field(
        "long_term", description="long_term 长期有效；temporary 短期有效（默认 7 天后失效），如“这周出差”"
    )
    supersedes_id: str | None = Field(None, description="如果这条记忆替代了某条旧记忆，填写旧记忆的 id")


class SaveMemoryTool(BaseTool):
    name = "save_memory"
    description = "保存一条关于用户的长期记忆。仅在用户明确要求记住，或用户明确陈述了需要长期记住的个人情况时使用。"
    Params = SaveMemoryParams
    side_effect = True

    async def run(self, params: SaveMemoryParams, ctx: ToolContext) -> Any:
        try:
            memory, created = await memory_service.add(
                ctx.user_id,
                kind=params.kind,
                content=params.content,
                scope=params.scope,
                status=ACTIVE,
                source="user_explicit",
                source_session_id=ctx.session_id,
                supersedes_id=params.supersedes_id,
            )
        except MemoryOpError as exc:
            raise ToolError(str(exc)) from exc
        return {"created": created, "memory": memory_to_dict(memory)}


class MemoryIdParams(BaseModel):
    memory_id: str = Field(min_length=1, max_length=64, description="记忆 id（系统提示词中方括号内的值）")


class ConfirmMemoryTool(BaseTool):
    name = "confirm_memory"
    description = "用户明确确认某条“待确认的记忆”属实时，将其转为长期记忆。"
    Params = MemoryIdParams
    side_effect = True

    async def run(self, params: MemoryIdParams, ctx: ToolContext) -> Any:
        try:
            memory = await memory_service.confirm(ctx.user_id, params.memory_id)
        except MemoryOpError as exc:
            raise ToolError(str(exc)) from exc
        return {"memory": memory_to_dict(memory)}


class ForgetMemoryTool(BaseTool):
    name = "forget_memory"
    description = "用户要求忘记某条记忆，或明确否认某条待确认记忆时，将其归档（不再使用）。"
    Params = MemoryIdParams
    side_effect = True

    async def run(self, params: MemoryIdParams, ctx: ToolContext) -> Any:
        try:
            memory = await memory_service.archive(ctx.user_id, params.memory_id)
        except MemoryOpError as exc:
            raise ToolError(str(exc)) from exc
        return {"memory": memory_to_dict(memory)}
