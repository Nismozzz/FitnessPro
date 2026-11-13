from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.service import SessionNotFound, agent_service
from app.agent.types import AgentEvent
from app.auth.security import get_current_user_id
from app.conversation import repository as repo

router = APIRouter(prefix="/agent", tags=["agent"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


def _sse(event: AgentEvent) -> str:
    data = json.dumps(asdict(event)["data"], ensure_ascii=False, default=str)
    return f"event: {event.type}\ndata: {data}\n\n"


@router.post("/chat")
async def chat(body: ChatRequest, user_id: str = Depends(get_current_user_id)) -> StreamingResponse:
    """SSE 流式对话。事件：session / context / content_reset / content / tool_call / tool_result / error / done。"""
    try:
        session_id = await agent_service.prepare_session(user_id, body.session_id, body.message)
    except SessionNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")

    queue = agent_service.start_turn(user_id=user_id, session_id=session_id, message=body.message)

    async def stream() -> AsyncIterator[str]:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield _sse(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    rows = await repo.load_messages(session_id)
    return {
        "session": {
            "id": session.id,
            "title": session.title,
            "summary": session.summary,
            "summary_upto_seq": session.summary_upto_seq,
        },
        "messages": [
            {
                "seq": r.seq,
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
    if not await repo.delete_session(user_id, session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
