from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.auth.security import get_current_user_id
from app.memory.service import (
    ACTIVE,
    MemoryKind,
    MemoryNotFound,
    MemoryOpError,
    MemoryScope,
    memory_service,
    memory_to_dict,
)

router = APIRouter(prefix="/memories", tags=["memories"])

StatusName = Literal["candidate", "active", "rejected", "archived"]


class MemoryCreate(BaseModel):
    kind: MemoryKind
    content: str = Field(min_length=2, max_length=200)
    scope: MemoryScope = "long_term"


class MemoryUpdate(BaseModel):
    content: str = Field(min_length=2, max_length=200)


def _raise(exc: MemoryOpError) -> None:
    if isinstance(exc, MemoryNotFound):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("")
async def list_memories(
    status_: list[StatusName] | None = Query(None, alias="status"),
    user_id: str = Depends(get_current_user_id),
) -> list[dict[str, Any]]:
    return [memory_to_dict(m) for m in await memory_service.list(user_id, status_)]


@router.post("", status_code=201)
async def create_memory(body: MemoryCreate, user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    try:
        memory, _ = await memory_service.add(
            user_id, kind=body.kind, content=body.content, scope=body.scope, status=ACTIVE, source="manual"
        )
    except MemoryOpError as exc:
        _raise(exc)
    return memory_to_dict(memory)


@router.post("/{memory_id}/confirm")
async def confirm_memory(memory_id: str, user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    try:
        return memory_to_dict(await memory_service.confirm(user_id, memory_id))
    except MemoryOpError as exc:
        _raise(exc)
        raise


@router.post("/{memory_id}/reject")
async def reject_memory(memory_id: str, user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    try:
        return memory_to_dict(await memory_service.reject(user_id, memory_id))
    except MemoryOpError as exc:
        _raise(exc)
        raise


@router.patch("/{memory_id}")
async def edit_memory(
    memory_id: str, body: MemoryUpdate, user_id: str = Depends(get_current_user_id)
) -> dict[str, Any]:
    try:
        return memory_to_dict(await memory_service.update_content(user_id, memory_id, body.content))
    except MemoryOpError as exc:
        _raise(exc)
        raise


@router.delete("/{memory_id}", status_code=204)
async def delete_memory(memory_id: str, user_id: str = Depends(get_current_user_id)) -> None:
    if not await memory_service.delete(user_id, memory_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
