from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EventType = Literal["session", "context", "content_reset", "content", "tool_call", "tool_result", "error", "done"]


@dataclass
class AgentEvent:
    """
    Agent 事件（SSE 中的一条 event）。

    终止协议：每次执行都以且仅以一个 done 事件结束，data.status 取值：
    completed / max_iterations / timeout / error。失败时 done 之前会先发 error。
    """

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    parse_error: str | None = None
