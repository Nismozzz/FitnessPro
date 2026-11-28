from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.observability.context import trace_scope
from app.observability.service import get_today_tokens

from app.api.deps import get_request_limits, rate_limit
from app.agent.service import agent_service, make_title
from app.agent.events import TurnEvents
from app.agent.types import AgentEvent
from app.agent.citations import history_citations
from app.auth.security import get_current_user_id
from app.config import settings
from app.database.models import AgentTurn
from app.llm.provider import Tier
from app.conversation import repository as repo
from app.request_limits import PREFLIGHT_TIMEOUT, RequestLimits
from app.utils.background import spawn
from app.utils.time import utcnow
from app.vision.images import IMAGE_MARKER, image_path, require_images

router = APIRouter(prefix="/agent", tags=["agent"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None
    model_tier: Tier = "normal"
    selected_tools: list[str] | None = Field(default=None, max_length=100)
    image_ids: list[str] = Field(default_factory=list, max_length=1)


def _sse(event: AgentEvent, sequence: int) -> str:
    data = json.dumps(asdict(event)["data"], ensure_ascii=False, default=str)
    return f"id: {sequence}\nevent: {event.type}\ndata: {data}\n\n"


def _event_stream(events: TurnEvents, request_id: str, after: int = 0) -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        async for sequence, event in events.subscribe(after):
            yield _sse(event, sequence)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": request_id},
    )


@router.get("/tools")
async def list_tools(user_id: str = Depends(get_current_user_id)) -> list[dict[str, Any]]:
    return agent_service.tool_catalog()


@router.post("/chat", dependencies=[Depends(rate_limit)])
async def chat(
    body: ChatRequest,
    user_id: str = Depends(get_current_user_id),
    limits: RequestLimits = Depends(get_request_limits),
) -> StreamingResponse:
    """SSE 流式对话。事件：session / context / content_reset / content / tool_call / tool_result / citations / error / done。"""
    return await _stream_turn(body, user_id, limits)


@router.post("/turns/{turn_id}/retry", dependencies=[Depends(rate_limit)])
async def retry_turn(
    turn_id: str,
    user_id: str = Depends(get_current_user_id),
    limits: RequestLimits = Depends(get_request_limits),
) -> StreamingResponse:
    turn = await repo.get_owned_turn(user_id, turn_id)
    if turn is None:
        raise HTTPException(404, "任务不存在")
    if turn.status not in repo.RETRYABLE_TURN_STATUSES:
        raise HTTPException(409, "本轮状态已变化，请刷新后查看")
    return await _stream_turn(ChatRequest.model_validate(turn.request), user_id, limits, previous=turn)


async def _stream_turn(
    body: ChatRequest, user_id: str, limits: RequestLimits, *, previous: AgentTurn | None = None,
) -> StreamingResponse:
    try:
        agent_service.select_tools(body.selected_tools)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if body.image_ids:
        from app.vision.service import is_available
        if not is_available():
            raise HTTPException(503, "图片识别尚未配置")
        if body.selected_tools is not None and "identify_equipment" not in body.selected_tools:
            raise HTTPException(422, "请先在工具选择中启用器械图片识别")
        try:
            require_images(user_id, body.image_ids)
        except FileNotFoundError:
            raise HTTPException(404, "图片不存在或已删除，请重新上传")
    message = body.message + "".join(f"\n[图片附件：{image_id}]" for image_id in body.image_ids)
    request_id = uuid4().hex
    queue = await asyncio.shield(spawn(
        _start_turn(body, user_id, limits, message, request_id, previous),
        name=f"accept-turn:{request_id}",
    ))
    if isinstance(queue, HTTPException):
        raise queue

    return _event_stream(queue, request_id)


async def _start_turn(
    body: ChatRequest, user_id: str, limits: RequestLimits, message: str, request_id: str,
    previous: AgentTurn | None,
) -> TurnEvents | HTTPException:
    lease = await limits.acquire_turn(user_id, settings.agent.total_timeout)
    if lease is None:
        return HTTPException(429, "请等待当前对话完成")

    handed_off = False
    turn = None
    try:
        async with asyncio.timeout(PREFLIGHT_TIMEOUT):
            if await get_today_tokens(user_id) >= settings.limits.daily_llm_tokens:
                tomorrow = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                return HTTPException(429, f"今日模型 Token 预算已用完，将于 {tomorrow:%Y-%m-%d %H:%M} UTC 重置")
        if previous is None:
            turn = await repo.create_turn(user_id, body.session_id, make_title(body.message),
                                          request_id, body.model_dump(mode="json"), message, lease.owner)
            if turn is None:
                return HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
        else:
            turn = await repo.retry_turn(user_id, previous, request_id, lease.owner)
        session_id = turn.session_id
        with trace_scope(request_id=request_id, user_id=user_id, session_id=session_id):
            queue = agent_service.start_turn(
                user_id=user_id, session_id=session_id, message=message, model_tier=body.model_tier,
                selected_tools=body.selected_tools, lease=lease, turn=turn,
            )
        handed_off = True
        return queue
    except repo.TurnConflict as exc:
        return HTTPException(409, str(exc))
    except TimeoutError:
        return HTTPException(503, "对话初始化超时，请稍后再试")
    finally:
        if not handed_off:
            try:
                if turn is not None:
                    await repo.finish_turn(turn, "interrupted", [])
            finally:
                if await lease.release() and turn is not None:
                    await repo.clear_turn_lease(turn.id, lease.owner)


@router.post("/turns/{request_id}/cancel")
async def cancel_turn(request_id: str, user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    cancelled = await agent_service.cancel_turn(user_id, request_id)
    return {"cancelled": cancelled, "status": "cancelled" if cancelled else "finished_or_not_found"}


def _turn_status(turn: AgentTurn) -> dict[str, Any]:
    return {
        "id": turn.id,
        "status": turn.status,
        "committed_tools": list(dict.fromkeys(receipt["tool"] for receipt in turn.writes.values())),
    }


@router.get("/turns/{turn_id}")
async def get_turn(turn_id: str, user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    turn = await repo.get_owned_turn(user_id, turn_id)
    if turn is None:
        raise HTTPException(404, "任务不存在")
    final = None
    if turn.status == "completed":
        rows = await repo.load_turn_messages(turn.session_id, turn.id)
        final = next((row for row in reversed(rows) if row.role == "assistant" and not row.tool_calls), None)
    return {
        **_turn_status(turn),
        "session_id": turn.session_id,
        "answer": final.content if final else None,
        "committed_results": list(turn.writes.values()),
    }


@router.get("/turns/{turn_id}/events")
async def reconnect_turn(
    turn_id: str, after: int = 0, user_id: str = Depends(get_current_user_id),
) -> StreamingResponse:
    if after < 0:
        raise HTTPException(422, "事件序号不能为负数")
    turn = await repo.get_owned_turn(user_id, turn_id)
    if turn is None:
        raise HTTPException(404, "任务不存在")
    events = agent_service.turn_events(turn.request_id)
    if events is None:
        raise HTTPException(409, "请从会话历史读取任务结果")
    return _event_stream(events, turn.request_id, after)


@router.get("/sessions")
async def list_sessions(user_id: str = Depends(get_current_user_id)) -> list[dict[str, Any]]:
    return [
        {
            "id": s.id,
            "title": s.title,
            "created_at": s.created_at.isoformat(),
            "updated_at": s.updated_at.isoformat(),
        }
        for s in await repo.list_sessions(user_id)
    ]


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str, user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    session = await repo.get_owned_session(user_id, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    latest = await repo.latest_turn(session_id)
    rows = await repo.load_messages(session_id)
    citations = history_citations(rows)
    return {
        "latest_turn": _turn_status(latest) if latest and rows and rows[-1].turn_id == latest.id else None,
        "session": {
            "id": session.id,
            "title": session.title,
            "summary": session.summary,
            "summary_upto_seq": session.summary_upto_seq,
        },
        "messages": [
            {
                "seq": r.seq,
                "citations": citations.get(r.seq, []),
                "turn_id": r.turn_id,
                "role": r.role,
                "content": r.content,
                "tool_calls": r.tool_calls,
                "tool_call_id": r.tool_call_id,
                "name": r.name,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, user_id: str = Depends(get_current_user_id)) -> None:
    if await repo.get_owned_session(user_id, session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    rows = await repo.load_messages(session_id)
    try:
        if not await repo.delete_session(user_id, session_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    except repo.TurnConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    # Session deletion also removes its photos; deleted photos stay unavailable in other references.
    for row in rows:
        if row.role == "user":
            for image_id in IMAGE_MARKER.findall(row.content or ""):
                image_path(user_id, image_id).unlink(missing_ok=True)
