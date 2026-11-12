"""
历史消息的分组、修复与预算选择（纯函数，便于测试）。

修正 CookHero 的几个上下文缺口（设计文档 3.3）：
- 以“轮”（turn）为单位处理历史，工具调用与结果不会被拆开；
- 从最新一轮往前连续选择，摘要覆盖 seq < summary_upto_seq 的部分，原文覆盖其余部分，
  两者之间没有缺口；预算不足且压缩失败时会显式告知模型丢弃了多少轮；
- 历史中的工具结果按预算截断；
- 中途失败留下的“有调用无结果”的工具消息会被修复，避免 API 报错。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from app.llm.tokens import message_tokens, truncate_to_tokens


class MessageRow(Protocol):
    seq: int
    turn_id: str
    role: str
    content: str | None
    tool_calls: list | None
    tool_call_id: str | None


@dataclass
class MessageGroup:
    turn_id: str
    first_seq: int
    last_seq: int
    messages: list[dict[str, Any]]
    tokens: int


def row_to_message(row: MessageRow, tool_result_tokens: int) -> dict[str, Any]:
    if row.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": row.tool_call_id,
            "content": truncate_to_tokens(row.content or "", tool_result_tokens),
        }
    if row.role == "assistant":
        msg: dict[str, Any] = {"role": "assistant", "content": row.content}
        if row.tool_calls:
            msg["tool_calls"] = row.tool_calls
        return msg
    return {"role": row.role, "content": row.content or ""}


def repair_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保证每个 tool_call 都有结果、每个工具结果都有对应调用。"""
    result_ids = {m.get("tool_call_id") for m in messages if m["role"] == "tool"}
    declared: set[str] = set()
    repaired: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            calls = [c for c in m["tool_calls"] if c.get("id") in result_ids]
            if calls:
                m = {**m, "tool_calls": calls}
                declared.update(c["id"] for c in calls)
            else:
                m = {k: v for k, v in m.items() if k != "tool_calls"}
                if not m.get("content"):
                    continue
            repaired.append(m)
        elif m["role"] == "tool":
            if m.get("tool_call_id") in declared:
                repaired.append(m)
        elif m["role"] == "assistant":
            repaired.append({**m, "content": m.get("content") or ""})
        else:
            repaired.append(m)
    return repaired


def rows_to_groups(rows: list[MessageRow], tool_result_tokens: int) -> list[MessageGroup]:
    raw_groups: list[tuple[str, list[MessageRow]]] = []
    for row in rows:
        if not raw_groups or raw_groups[-1][0] != row.turn_id:
            raw_groups.append((row.turn_id, []))
        raw_groups[-1][1].append(row)

    groups = []
    for turn_id, group_rows in raw_groups:
        messages = repair_messages([row_to_message(r, tool_result_tokens) for r in group_rows])
        if not messages:
            continue
        groups.append(
            MessageGroup(
                turn_id=turn_id,
                first_seq=group_rows[0].seq,
                last_seq=group_rows[-1].seq,
                messages=messages,
                tokens=sum(message_tokens(m) for m in messages),
            )
        )
    return groups


def fit_recent_groups(
    groups: list[MessageGroup], budget: int
) -> tuple[list[MessageGroup], int]:
    """从最新一轮往前连续选择，直到超出预算。最新一轮总是保留。返回 (保留的组, 丢弃的组数)。"""
    kept: list[MessageGroup] = []
    used = 0
    for g in reversed(groups):
        if kept and used + g.tokens > budget:
            break
        kept.append(g)
        used += g.tokens
    kept.reverse()
    return kept, len(groups) - len(kept)


def serialize_groups(groups: list[MessageGroup], tool_tokens: int = 200) -> str:
    """把若干轮对话转成摘要模型可读的文本，保留工具名和参数。"""
    lines: list[str] = []
    for g in groups:
        call_names: dict[str, str] = {}
        for m in g.messages:
            role = m["role"]
            if role == "user":
                lines.append(f"[用户] {m.get('content') or ''}")
            elif role == "assistant":
                if m.get("content"):
                    lines.append(f"[教练] {m['content']}")
                for call in m.get("tool_calls") or []:
                    fn = call.get("function", {})
                    call_names[call.get("id", "")] = fn.get("name", "")
                    lines.append(f"[教练调用工具] {fn.get('name')}({fn.get('arguments', '')})")
            elif role == "tool":
                name = call_names.get(m.get("tool_call_id") or "", "tool")
                content = m.get("content") or ""
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                lines.append(f"[工具结果 {name}] {truncate_to_tokens(content, tool_tokens)}")
        lines.append("---")
    return "\n".join(lines)
