"""
滚动摘要压缩。

- 以轮为单位，从最早的未压缩轮开始并入摘要，至少保留 min_recent_turns 轮原文；
- 同一会话的压缩串行执行（进程内锁），摘要写回使用版本号乐观锁；
- 原始消息永远保留在数据库中，只移动 summary_upto_seq 指针。
"""

from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.context.history import rows_to_groups, serialize_groups
from app.context.prompts import COMPRESS_SYSTEM, COMPRESS_USER
from app.conversation import repository as repo
from app.llm.provider import get_llm
from app.llm.tokens import truncate_to_tokens

logger = logging.getLogger(__name__)


class ContextCompressor:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def uncompressed_tokens(self, session_id: str) -> int:
        session = await repo.get_session(session_id)
        if session is None:
            return 0
        rows = await repo.load_messages(session_id, session.summary_upto_seq)
        groups = rows_to_groups(rows, settings.context.history_tool_result_tokens)
        return sum(g.tokens for g in groups)

    async def compress(self, session_id: str, *, target_tokens: int) -> bool:
        cfg = settings.context
        async with self._lock(session_id):
            session = await repo.get_session(session_id)
            if session is None:
                return False
            rows = await repo.load_messages(session_id, session.summary_upto_seq)
            groups = rows_to_groups(rows, cfg.history_tool_result_tokens)
            remaining = sum(g.tokens for g in groups)
            if remaining <= target_tokens:
                return False

            compressible = max(0, len(groups) - cfg.min_recent_turns)
            to_compress = []
            for g in groups[:compressible]:
                if remaining <= target_tokens:
                    break
                to_compress.append(g)
                remaining -= g.tokens
            if not to_compress:
                return False
            new_upto = groups[len(to_compress)].first_seq

            conversation = truncate_to_tokens(serialize_groups(to_compress), 12000)
            try:
                summary = await get_llm("fast").complete_text(
                    [
                        {
                            "role": "system",
                            "content": COMPRESS_SYSTEM.format(max_chars=cfg.summary_max_tokens),
                        },
                        {
                            "role": "user",
                            "content": COMPRESS_USER.format(
                                previous=session.summary or "（无）", conversation=conversation
                            ),
                        },
                    ]
                )
            except Exception as exc:
                logger.warning("会话 %s 摘要压缩失败：%s", session_id, exc)
                return False

            summary = summary.strip()
            if not summary:
                return False
            ok = await repo.update_summary(session_id, session.version, summary, new_upto)
            if ok:
                logger.info(
                    "会话 %s 压缩 %d 轮，summary_upto_seq=%d", session_id, len(to_compress), new_upto
                )
            return ok

    async def maybe_compress(self, session_id: str) -> bool:
        """回答结束后的后台压缩：超过触发阈值时压缩到目标长度。"""
        cfg = settings.context
        if await self.uncompressed_tokens(session_id) <= cfg.compress_trigger_tokens:
            return False
        return await self.compress(
            session_id, target_tokens=int(cfg.history_token_budget * cfg.compress_target_ratio)
        )


context_compressor = ContextCompressor()
