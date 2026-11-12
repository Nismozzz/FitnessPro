"""
Agent 执行循环（Function Calling 风格的 ReAct）。

    调用 LLM（流式）
      ├─ 返回工具调用 → 校验并执行 → 结果追加到 messages → 下一轮
      └─ 无工具调用   → 输出最终回答 → done(completed)

相对 CookHero 的改进：
- run 只有流式一种实现，终止协议统一：每次执行以且仅以一个 done 事件结束；
- 最后一轮强制 tool_choice="none"，让模型基于已有信息给出回答，而不是直接报错；
- 除轮数上限外还有总时长预算，并把剩余时间作为每次 LLM 请求的超时；
- 流式工具参数解析失败时把错误反馈给模型重试，而不是带着空参数执行；
- 兼容 DeepSeek 思考模式：同一轮内把 reasoning_content 回传给模型（不落库）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.agent.tools.base import ToolContext, ToolExecutor
from app.agent.types import AgentEvent, ToolCall
from app.config import AgentSettings
from app.llm.provider import LLMClient

logger = logging.getLogger(__name__)


def _parse_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    text = raw.strip() or "{}"
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, str(exc)
    if not isinstance(value, dict):
        return {}, "参数必须是 JSON 对象"
    return value, None


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        executor: ToolExecutor,
        cfg: AgentSettings,
        tool_result_max_tokens: int = 2500,
    ):
        self.llm = llm
        self.executor = executor
        self.cfg = cfg
        self.tool_result_max_tokens = tool_result_max_tokens

    async def run(
        self,
        messages: list[dict[str, Any]],
        ctx: ToolContext,
        transcript: list[dict[str, Any]],
    ) -> AsyncIterator[AgentEvent]:
        """
        messages：本次执行的工作上下文（会被追加）。
        transcript：需要持久化的新消息（assistant / tool），由调用方保存。
        """
        tools = self.executor.schemas() or None
        deadline = time.monotonic() + self.cfg.total_timeout
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        tool_calls_count = 0

        for iteration in range(1, self.cfg.max_iterations + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                yield AgentEvent("error", {"message": "本次任务处理超时"})
                yield self._done("timeout", iteration - 1, usage, tool_calls_count)
                return

            last_round = iteration == self.cfg.max_iterations
            overrides: dict[str, Any] = {"timeout": remaining}
            if tools and last_round:
                overrides["tool_choice"] = "none"

            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            pending: dict[int, dict[str, str]] = {}
            try:
                async for chunk in self.llm.stream(messages, tools=tools, **overrides):
                    if chunk.usage:
                        usage["prompt_tokens"] += chunk.usage.prompt_tokens or 0
                        usage["completion_tokens"] += chunk.usage.completion_tokens or 0
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    if delta.content:
                        content_parts.append(delta.content)
                        yield AgentEvent("content", {"delta": delta.content})
                    for tc in delta.tool_calls or []:
                        index = tc.index if tc.index is not None else len(pending)
                        slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function is not None:
                            if tc.function.name:
                                slot["name"] = tc.function.name
                            if tc.function.arguments:
                                slot["arguments"] += tc.function.arguments
            except Exception as exc:
                logger.exception("LLM 调用失败 session=%s iteration=%d", ctx.session_id, iteration)
                partial = "".join(content_parts)
                if partial:
                    transcript.append({"role": "assistant", "content": partial})
                yield AgentEvent("error", {"message": f"模型调用失败：{type(exc).__name__}"})
                yield self._done("error", iteration, usage, tool_calls_count)
                return

            content = "".join(content_parts)

            if not pending:
                transcript.append({"role": "assistant", "content": content})
                yield self._done("completed", iteration, usage, tool_calls_count)
                return

            if last_round:
                # 已禁止工具调用但模型仍然返回了调用：结束本轮，保留已有文本
                transcript.append(
                    {"role": "assistant", "content": content or "（本次任务步骤过多，未能完成，请缩小问题范围后重试）"}
                )
                yield AgentEvent("error", {"message": "达到最大推理轮数"})
                yield self._done("max_iterations", iteration, usage, tool_calls_count)
                return

            calls: list[ToolCall] = []
            for index in sorted(pending):
                slot = pending[index]
                args, error = _parse_arguments(slot["arguments"])
                calls.append(
                    ToolCall(
                        id=slot["id"] or f"call_{uuid.uuid4().hex[:12]}",
                        name=slot["name"],
                        arguments=args,
                        raw_arguments=slot["arguments"],
                        parse_error=error,
                    )
                )

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.raw_arguments or "{}"},
                    }
                    for c in calls
                ],
            }
            working_msg = dict(assistant_msg)
            if reasoning_parts:
                working_msg["reasoning_content"] = "".join(reasoning_parts)
            messages.append(working_msg)
            transcript.append(assistant_msg)

            for call in calls:
                tool_calls_count += 1
                yield AgentEvent(
                    "tool_call", {"id": call.id, "name": call.name, "arguments": call.arguments}
                )
                started = time.monotonic()
                result = await self.executor.execute(call, ctx)
                content_str = result.to_content(self.tool_result_max_tokens)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content_str})
                transcript.append(
                    {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content_str}
                )
                yield AgentEvent(
                    "tool_result",
                    {
                        "id": call.id,
                        "name": call.name,
                        "success": result.success,
                        "error": result.error,
                        "data": result.data,
                        "ms": int((time.monotonic() - started) * 1000),
                    },
                )

        # for 循环正常结束只可能发生在 max_iterations <= 0 的配置下
        yield AgentEvent("error", {"message": "max_iterations 配置无效"})
        yield self._done("error", 0, usage, tool_calls_count)

    @staticmethod
    def _done(status: str, iterations: int, usage: dict, tool_calls: int) -> AgentEvent:
        return AgentEvent(
            "done",
            {"status": status, "iterations": iterations, "tool_calls": tool_calls, "usage": usage},
        )
