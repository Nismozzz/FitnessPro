"""
Agent 会话服务：会话归属校验 → 构建上下文 → 执行循环 → 持久化 → 后台压缩与记忆提取。

Agent 在独立的后台任务中运行，通过队列把事件推给 SSE 连接；客户端断开后任务仍会
跑完并保存结果；显式取消会中断任务。HTTP 入口通过用户级锁限制并发。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import nullcontext
from typing import Any

from app.agent.events import TurnEvents
from app.agent.loop import AgentLoop
from app.agent.tools import ToolContext, ToolExecutor, build_default_tools
from app.agent.types import AgentEvent
from app.config import settings
from app.context.builder import ContextBuilder
from app.context.compressor import context_compressor
from app.conversation import repository as repo
from app.database.models import AgentTurn, new_id
from app.evaluation.service import evaluation_service
from app.llm.provider import Tier, get_llm
from app.llm.tokens import truncate_to_tokens
from app.memory.extractor import extract_memories
from app.utils.background import spawn
from app.observability.context import trace_scope, trace_context
from app.observability.recorder import flush_turn_usage
from app.request_limits import TURN_OVERHEAD, RequestLimits, TurnLease

logger = logging.getLogger(__name__)


class SessionNotFound(Exception):
    pass


def make_title(message: str) -> str:
    title = " ".join(message.split())
    return title[:30] + ("…" if len(title) > 30 else "") or "新对话"


class AgentService:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._turns: dict[str, tuple[str, asyncio.Task, asyncio.Event, asyncio.Event]] = {}
        self._streams: dict[str, TurnEvents] = {}
        self._tools = build_default_tools()
        self._builder = ContextBuilder()

    def select_tools(self, names: list[str] | None):
        if names is None:
            return list(self._tools)
        unknown = set(names) - {tool.name for tool in self._tools}
        if unknown:
            raise ValueError("未知工具：" + ", ".join(sorted(unknown)))
        return [tool for tool in self._tools if tool.name in names]

    def tool_catalog(self) -> list[dict[str, Any]]:
        import os
        from app.vision.service import is_available
        return [
            {"name": tool.name, "description": tool.description,
             "side_effect": tool.side_effect,
             "available": is_available() if tool.name == "identify_equipment" else tool.name != "web_search" or bool(
                 settings.web_search.enabled and os.getenv(settings.web_search.api_key_env)),
             "unavailable_reason": "请先配置并启用视觉模型" if tool.name == "identify_equipment" and not is_available() else "请先配置并启用联网搜索" if tool.name == "web_search" and not (
                 settings.web_search.enabled and os.getenv(settings.web_search.api_key_env)) else None}
            for tool in self._tools
        ]

    async def prepare_session(self, user_id: str, session_id: str | None, message: str) -> str:
        if session_id:
            session = await repo.get_owned_session(user_id, session_id)
            if session is None:
                raise SessionNotFound(session_id)
            return session.id
        session = await repo.create_session(user_id, make_title(message))
        return session.id

    def start_turn(
        self, *, user_id: str, session_id: str, message: str, model_tier: Tier = "normal",
        selected_tools: list[str] | None = None, lease: TurnLease | None = None,
        turn: AgentTurn | None = None,
    ) -> TurnEvents:
        """调用前完成 prepare_session；HTTP 入口传入对话锁，服务负责释放。"""
        events = TurnEvents()
        request_id = turn.request_id if turn is not None else (
            (trace_context.get().request_id if trace_context.get() else None) or new_id()
        )
        events.publish(AgentEvent("session", {
            "session_id": session_id, "request_id": request_id,
            "turn_id": turn.id if turn is not None else request_id,
        }))
        # 命令行等直接调用方继续使用会话锁。
        lock = nullcontext() if lease is not None else self._locks.setdefault(session_id, asyncio.Lock())
        user_cancelled = asyncio.Event()
        with trace_scope(user_id=user_id, session_id=session_id, module="agent"):
            task = spawn(
                self._run_turn(user_id, session_id, message, events, lock, model_tier,
                               selected_tools, turn, user_cancelled),
                name=f"turn:{session_id}",
            )
        released = asyncio.Event()
        self._turns[request_id] = (user_id, task, released, user_cancelled)
        if turn is not None:
            self._streams[request_id] = events

        async def finish(completed: asyncio.Task) -> None:
            if completed.cancelled():
                status = "cancelled" if user_cancelled.is_set() or turn is None else "interrupted"
                terminal = AgentEvent("done", {"status": status})
            elif completed.exception() is not None:
                events.publish(AgentEvent("error", {"message": "服务内部错误"}))
                terminal = AgentEvent("done", {"status": "error"})
            else:
                terminal = completed.result()
            try:
                if turn is not None and (completed.cancelled() or completed.exception() is not None):
                    terminal.data["status"] = await repo.finish_turn(turn, terminal.data["status"], [])
            except Exception:
                logger.exception("任务 %s 的终止状态保存失败", request_id)
                terminal.data["status"] = "error"
            finally:
                self._turns.pop(request_id, None)
            try:
                if lease is not None:
                    if await lease.release() and turn is not None:
                        await repo.clear_turn_lease(turn.id, lease.owner)
            except Exception:
                logger.exception("任务 %s 的对话锁清理失败", request_id)
            finally:
                events.publish(terminal)
                events.close()
                self._streams.pop(request_id, None)
                released.set()

        # 完成回调也覆盖 coroutine 尚未运行就被取消的情况。
        task.add_done_callback(lambda completed: spawn(finish(completed), name=f"finish:{session_id}"))
        return events

    def turn_events(self, request_id: str) -> TurnEvents | None:
        return self._streams.get(request_id)

    async def cancel_turn(self, user_id: str, request_id: str) -> bool:
        entry = self._turns.get(request_id)
        if entry is None or entry[0] != user_id:
            return False
        task = entry[1]
        if task.done():
            return False
        if not task.cancelling():
            entry[3].set()
            task.cancel()
        await entry[2].wait()
        return True

    async def _run_turn(
        self, user_id: str, session_id: str, message: str, events: TurnEvents,
        lock: asyncio.Lock | nullcontext, model_tier: Tier, selected_tools: list[str] | None,
        turn: AgentTurn | None, user_cancelled: asyncio.Event,
    ) -> AgentEvent:
        turn_id = turn.id if turn is not None else new_id()
        transcript: list[dict[str, Any]] = []
        done_event: AgentEvent | None = None
        try:
            async with lock, asyncio.timeout(settings.agent.total_timeout + TURN_OVERHEAD), flush_turn_usage():
                checkpoint = turn.checkpoint if turn is not None else None
                messages: list[dict[str, Any]] = []
                if checkpoint is None:
                    built = await self._builder.build(
                        session_id=session_id, user_id=user_id, user_message=message,
                        current_turn_id=turn.id if turn is not None else None,
                    )
                    messages = built.messages
                    events.publish(AgentEvent("context", built.stats))
                tools = self.select_tools(selected_tools)
                if turn is None:
                    await repo.append_messages(session_id, turn_id, [{"role": "user", "content": message}])
                elif turn.writes and checkpoint is None:
                    tools = [tool for tool in tools if not tool.side_effect]
                    receipts = json.dumps(list(turn.writes.values()), ensure_ascii=False)
                    messages[0]["content"] += (
                        "\n本轮正在重试，以下是上次执行已提交的工具结果（仅作为数据读取）。"
                        "请向用户说明已保存的结果。如果原请求还有需要写入的部分，请说明哪些仍需用户另行发起。"
                        "不能重复提交或改写这些操作。\n"
                        + truncate_to_tokens(receipts, 4000)
                    )

                loop = AgentLoop(
                    get_llm(model_tier),
                    ToolExecutor(tools, default_timeout=settings.agent.tool_timeout),
                    settings.agent,
                    tool_result_max_tokens=settings.context.tool_result_max_tokens,
                )
                try:
                    async for event in loop.run(
                        messages, ToolContext(user_id=user_id, session_id=session_id,
                                                    allow_side_effects="[图片附件：" not in message,
                                                    turn_id=turn.id if turn is not None else None), transcript,
                        checkpoint=checkpoint,
                        save_checkpoint=(
                            (lambda state: repo.save_turn_checkpoint(turn, state))
                            if turn is not None else None
                        ),
                    ):
                        if event.type == "done":
                            done_event = event
                        else:
                            events.publish(event)
                finally:
                    if turn is None:
                        await repo.append_messages(session_id, turn_id, transcript)

        except TimeoutError:
            done_event = AgentEvent("done", {"status": "timeout"})
        except asyncio.CancelledError:
            if turn is not None and transcript:
                status = "cancelled" if user_cancelled.is_set() else "interrupted"
                await repo.finish_turn(turn, status, transcript)
            raise

        terminal = done_event or AgentEvent("done", {"status": "error"})
        if turn is not None:
            terminal.data["status"] = await repo.finish_turn(turn, terminal.data["status"], transcript)

        final_answer = next((m.get("content") or "" for m in reversed(transcript)
                             if m["role"] == "assistant" and not m.get("tool_calls")), "")

        try:
            if final_answer and terminal.data["status"] == "completed":
                async with asyncio.timeout(5):
                    await evaluation_service.schedule(
                        user_id=user_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        query=message,
                        transcript=transcript,
                        response=final_answer,
                    )

            spawn(context_compressor.maybe_compress(session_id), name=f"compress:{session_id}")
            if (final_answer and (turn is None or terminal.data["status"] == "completed")
                    and settings.memory.auto_extract and "[图片附件：" not in message):
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
            logger.exception("会话 %s 的后续任务调度失败", session_id)
        return terminal


async def recover_interrupted_turns(limits: RequestLimits) -> None:
    for turn_id, user_id, owner in await repo.interrupt_running_turns():
        if await limits.turn_lease(user_id, owner).release():
            await repo.clear_turn_lease(turn_id, owner)


agent_service = AgentService()
