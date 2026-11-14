"""
Agent 会话服务：会话归属校验 → 构建上下文 → 执行循环 → 持久化 → 后台压缩与记忆提取。

Agent 在独立的后台任务中运行，通过队列把事件推给 SSE 连接；客户端断开后任务仍会
跑完并保存结果（进程重启后的恢复不在本阶段范围内）。同一会话的多轮请求用锁串行化，
保证消息 seq 连续、上下文一致。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agent.loop import AgentLoop
from app.agent.tools import ToolContext, ToolExecutor, build_default_tools
from app.agent.types import AgentEvent
from app.config import settings
from app.context.builder import ContextBuilder
from app.context.compressor import context_compressor
from app.conversation import repository as repo
from app.database.models import new_id
from app.evaluation.service import evaluation_service
from app.llm.provider import get_llm
from app.memory.extractor import extract_memories
from app.utils.background import spawn

logger = logging.getLogger(__name__)


class SessionNotFound(Exception):
    pass


def make_title(message: str) -> str:
    title = " ".join(message.split())
    return title[:30] + ("…" if len(title) > 30 else "") or "新对话"


class AgentService:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._tools = build_default_tools()
        self._builder = ContextBuilder()

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def prepare_session(self, user_id: str, session_id: str | None, message: str) -> str:
        if session_id:
            session = await repo.get_owned_session(user_id, session_id)
            if session is None:
                raise SessionNotFound(session_id)
            return session.id
        session = await repo.create_session(user_id, make_title(message))
        return session.id

    def start_turn(
        self, *, user_id: str, session_id: str, message: str
    ) -> asyncio.Queue[AgentEvent | None]:
        """启动一轮对话，返回事件队列；队列以 None 结尾。调用前必须完成 prepare_session。"""
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        spawn(self._run_turn(user_id, session_id, message, queue), name=f"turn:{session_id}")
        return queue

    async def _run_turn(
        self, user_id: str, session_id: str, message: str, queue: asyncio.Queue
    ) -> None:
        terminal_sent = False
        final_answer = ""
        try:
            async with self._lock(session_id):
                await queue.put(AgentEvent("session", {"session_id": session_id}))

                built = await self._builder.build(
                    session_id=session_id, user_id=user_id, user_message=message
                )
                await queue.put(AgentEvent("context", built.stats))

                turn_id = new_id()
                await repo.append_messages(session_id, turn_id, [{"role": "user", "content": message}])

                loop = AgentLoop(
                    get_llm("normal"),
                    ToolExecutor(self._tools, default_timeout=settings.agent.tool_timeout),
                    settings.agent,
                    tool_result_max_tokens=settings.context.tool_result_max_tokens,
                )
                transcript: list[dict[str, Any]] = []
                done_event: AgentEvent | None = None
                try:
                    async for event in loop.run(
                        built.messages, ToolContext(user_id=user_id, session_id=session_id), transcript
                    ):
                        if event.type == "done":
                            done_event = event
                        else:
                            await queue.put(event)
                finally:
                    # 无论成功与否都保存已产生的消息；不完整的工具调用会在下次构建上下文时被修复
                    await repo.append_messages(session_id, turn_id, transcript)

                for m in reversed(transcript):
                    if m["role"] == "assistant" and not m.get("tool_calls"):
                        final_answer = m.get("content") or ""
                        break

                await queue.put(done_event or AgentEvent("done", {"status": "error"}))
                terminal_sent = True

            if final_answer and done_event is not None and done_event.data.get("status") == "completed":
                await evaluation_service.schedule(
                    user_id=user_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    query=message,
                    transcript=transcript,
                    response=final_answer,
                )

            spawn(context_compressor.maybe_compress(session_id), name=f"compress:{session_id}")
            if final_answer and settings.memory.auto_extract:
                spawn(
                    extract_memories(
                        user_id=user_id,
                        session_id=session_id,
                        user_message=message,
                        assistant_message=final_answer,
                    ),
                    name=f"memory:{session_id}",
                )
        except Exception:
            logger.exception("会话 %s 处理失败", session_id)
            if not terminal_sent:
                await queue.put(AgentEvent("error", {"message": "服务内部错误"}))
                await queue.put(AgentEvent("done", {"status": "error"}))
        finally:
            await queue.put(None)


agent_service = AgentService()
