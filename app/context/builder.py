"""
上下文构建：每次请求从数据库重建模型输入。

    系统提示词（角色与规则 + 当前时间 + 用户画像 + 长期记忆 + 历史摘要）
    + 未压缩的近期历史（按轮连续选择，受 token 预算约束）
    + 当前用户消息

原文历史超过预算时先同步压缩；压缩失败时丢弃最早的若干轮，并显式提示模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.prompts import DROPPED_NOTE, SUMMARY_SECTION, SYSTEM_PROMPT
from app.config import settings
from app.context.compressor import ContextCompressor, context_compressor
from app.context.history import fit_recent_groups, rows_to_groups
from app.conversation import repository as repo
from app.llm.tokens import message_tokens
from app.memory.service import memory_service, render_memories
from app.users.profile import get_profile, render_profile
from app.utils.time import format_local, local_now


@dataclass
class BuiltContext:
    messages: list[dict[str, Any]]
    stats: dict[str, Any] = field(default_factory=dict)


class ContextBuilder:
    def __init__(self, compressor: ContextCompressor | None = None):
        self.compressor = compressor or context_compressor

    async def build(self, *, session_id: str, user_id: str, user_message: str) -> BuiltContext:
        cfg = settings.context

        profile = await get_profile(user_id)
        active, candidates = await memory_service.for_prompt(user_id)
        memories_text = render_memories(active, candidates, cfg.memory_prompt_tokens)

        session = await repo.get_session(session_id)
        if session is None:
            raise ValueError(f"会话不存在：{session_id}")
        rows = await repo.load_messages(session_id, session.summary_upto_seq)
        groups = rows_to_groups(rows, cfg.history_tool_result_tokens)

        compressed_now = False
        if sum(g.tokens for g in groups) > cfg.history_token_budget:
            compressed_now = await self.compressor.compress(
                session_id,
                target_tokens=int(cfg.history_token_budget * cfg.compress_target_ratio),
            )
            if compressed_now:
                session = await repo.get_session(session_id)
                assert session is not None
                rows = await repo.load_messages(session_id, session.summary_upto_seq)
                groups = rows_to_groups(rows, cfg.history_tool_result_tokens)

        kept, dropped = fit_recent_groups(groups, cfg.history_token_budget)

        system = SYSTEM_PROMPT.format(
            now=format_local(local_now(settings.timezone)),
            profile=render_profile(profile),
            memories=("\n\n" + memories_text) if memories_text else "",
            summary=SUMMARY_SECTION.format(summary=session.summary) if session.summary else "",
        )
        if dropped:
            system += DROPPED_NOTE.format(count=dropped)

        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for g in kept:
            messages.extend(g.messages)
        messages.append({"role": "user", "content": user_message})

        return BuiltContext(
            messages=messages,
            stats={
                "system_tokens": message_tokens(messages[0]),
                "history_turns": len(kept),
                "history_tokens": sum(g.tokens for g in kept),
                "dropped_turns": dropped,
                "has_summary": bool(session.summary),
                "compressed_now": compressed_now,
                "active_memories": len(active),
                "candidate_memories": len(candidates),
            },
        )
