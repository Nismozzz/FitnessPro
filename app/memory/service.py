"""
长期事实记忆。

- 状态机：candidate（自动提取，待确认）→ active / rejected；active → archived（遗忘或被新事实替代）
- 来源：user_explicit（用户明确要求记住）/ auto_extracted / manual（用户在界面添加）
- scope=temporary 的记忆带有效期，过期后不再进入上下文
- 所有读写都限定 user_id，防止跨用户访问
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import delete, or_, select

from app.config import settings
from app.database.models import Memory
from app.database.session import SessionLocal
from app.llm.tokens import truncate_to_tokens
from app.utils.time import utcnow

MemoryKind = Literal["injury", "goal", "preference", "equipment", "schedule", "body", "other"]
MemoryScope = Literal["long_term", "temporary"]

KIND_LABELS = {
    "injury": "伤病/身体限制",
    "goal": "目标",
    "preference": "偏好",
    "equipment": "器械条件",
    "schedule": "时间安排",
    "body": "身体数据",
    "other": "其他",
}

CANDIDATE = "candidate"
ACTIVE = "active"
REJECTED = "rejected"
ARCHIVED = "archived"


class MemoryOpError(Exception):
    pass


class MemoryNotFound(MemoryOpError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def memory_to_dict(m: Memory) -> dict[str, Any]:
    return {
        "id": m.id,
        "kind": m.kind,
        "content": m.content,
        "scope": m.scope,
        "status": m.status,
        "source": m.source,
        "supersedes_id": m.supersedes_id,
        "expires_at": m.expires_at.isoformat() if m.expires_at else None,
        "confirmed_at": m.confirmed_at.isoformat() if m.confirmed_at else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


class MemoryService:
    async def _owned(self, db, user_id: str, memory_id: str) -> Memory | None:
        stmt = select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id)
        return (await db.execute(stmt)).scalar_one_or_none()

    async def list(self, user_id: str, statuses: Iterable[str] | None = None) -> list[Memory]:
        async with SessionLocal() as db:
            stmt = select(Memory).where(Memory.user_id == user_id)
            if statuses:
                stmt = stmt.where(Memory.status.in_(list(statuses)))
            stmt = stmt.order_by(Memory.updated_at.desc())
            return list((await db.execute(stmt)).scalars().all())

    async def add(
        self,
        user_id: str,
        *,
        kind: str,
        content: str,
        scope: str = "long_term",
        status: str = CANDIDATE,
        source: str,
        source_session_id: str | None = None,
        supersedes_id: str | None = None,
    ) -> tuple[Memory, bool]:
        """新增记忆，返回 (记忆, 是否新建)。内容重复时返回已有记录。"""
        if kind not in KIND_LABELS:
            raise MemoryOpError(f"未知记忆类型：{kind}")
        content = content.strip()[:500]
        if not content:
            raise MemoryOpError("记忆内容不能为空")
        now = utcnow()

        async with SessionLocal() as db:
            if supersedes_id and await self._owned(db, user_id, supersedes_id) is None:
                supersedes_id = None

            existing = (
                await db.execute(
                    select(Memory).where(
                        Memory.user_id == user_id,
                        Memory.status.in_([ACTIVE, CANDIDATE]),
                    )
                )
            ).scalars().all()
            for m in existing:
                if _norm(m.content) == _norm(content):
                    if status == ACTIVE and m.status == CANDIDATE:
                        m.status = ACTIVE
                        m.confirmed_at = now
                        m.updated_at = now
                        await db.commit()
                    return m, False

            memory = Memory(
                user_id=user_id,
                kind=kind,
                content=content,
                scope=scope,
                status=status,
                source=source,
                source_session_id=source_session_id,
                supersedes_id=supersedes_id,
                expires_at=(
                    now + timedelta(days=settings.memory.temporary_ttl_days)
                    if scope == "temporary"
                    else None
                ),
                confirmed_at=now if status == ACTIVE else None,
                created_at=now,
                updated_at=now,
            )
            db.add(memory)
            if status == ACTIVE and supersedes_id:
                await self._archive_superseded(db, user_id, supersedes_id, now)
            await db.commit()
            return memory, True

    async def _archive_superseded(self, db, user_id: str, memory_id: str, now) -> None:
        old = await self._owned(db, user_id, memory_id)
        if old is not None and old.status in (ACTIVE, CANDIDATE):
            old.status = ARCHIVED
            old.updated_at = now

    async def confirm(self, user_id: str, memory_id: str) -> Memory:
        async with SessionLocal() as db:
            m = await self._owned(db, user_id, memory_id)
            if m is None:
                raise MemoryNotFound("记忆不存在")
            if m.status == ACTIVE:
                return m
            if m.status != CANDIDATE:
                raise MemoryOpError("只能确认“待确认”状态的记忆")
            now = utcnow()
            m.status = ACTIVE
            m.confirmed_at = now
            m.updated_at = now
            if m.supersedes_id:
                await self._archive_superseded(db, user_id, m.supersedes_id, now)
            await db.commit()
            return m

    async def _set_status(self, user_id: str, memory_id: str, status: str, allowed: set[str]) -> Memory:
        async with SessionLocal() as db:
            m = await self._owned(db, user_id, memory_id)
            if m is None:
                raise MemoryNotFound("记忆不存在")
            if m.status not in allowed:
                raise MemoryOpError(f"当前状态 {m.status} 不能变更为 {status}")
            m.status = status
            m.updated_at = utcnow()
            await db.commit()
            return m

    async def reject(self, user_id: str, memory_id: str) -> Memory:
        return await self._set_status(user_id, memory_id, REJECTED, {CANDIDATE})

    async def archive(self, user_id: str, memory_id: str) -> Memory:
        return await self._set_status(user_id, memory_id, ARCHIVED, {ACTIVE, CANDIDATE})

    async def update_content(self, user_id: str, memory_id: str, content: str) -> Memory:
        content = content.strip()[:500]
        if not content:
            raise MemoryOpError("记忆内容不能为空")
        async with SessionLocal() as db:
            m = await self._owned(db, user_id, memory_id)
            if m is None:
                raise MemoryNotFound("记忆不存在")
            m.content = content
            m.updated_at = utcnow()
            await db.commit()
            return m

    async def delete(self, user_id: str, memory_id: str) -> bool:
        async with SessionLocal() as db:
            result = await db.execute(
                delete(Memory).where(Memory.id == memory_id, Memory.user_id == user_id)
            )
            await db.commit()
            return result.rowcount > 0

    async def for_prompt(self, user_id: str) -> tuple[list[Memory], list[Memory]]:
        """返回 (已确认记忆, 近期待确认记忆)，均排除已过期的临时记忆。"""
        cfg = settings.memory
        now = utcnow()
        not_expired = or_(Memory.expires_at.is_(None), Memory.expires_at > now)
        async with SessionLocal() as db:
            active = (
                await db.execute(
                    select(Memory)
                    .where(Memory.user_id == user_id, Memory.status == ACTIVE, not_expired)
                    .order_by(Memory.kind, Memory.updated_at.desc())
                    .limit(cfg.max_active_in_prompt)
                )
            ).scalars().all()
            candidates = (
                await db.execute(
                    select(Memory)
                    .where(
                        Memory.user_id == user_id,
                        Memory.status == CANDIDATE,
                        not_expired,
                        Memory.created_at >= now - timedelta(days=cfg.candidate_window_days),
                    )
                    .order_by(Memory.created_at.desc())
                    .limit(cfg.max_candidates_in_prompt)
                )
            ).scalars().all()
        return list(active), list(candidates)


def render_memories(active: list[Memory], candidates: list[Memory], max_tokens: int) -> str:
    lines: list[str] = []
    if active:
        lines.append("## 关于用户的长期记忆（用户已确认）")
        for m in active:
            suffix = f"（临时，{m.expires_at:%m-%d} 前有效）" if m.expires_at else ""
            lines.append(f"- [{m.id}] {KIND_LABELS.get(m.kind, m.kind)}：{m.content}{suffix}")
    if candidates:
        lines.append(
            "## 待确认的记忆（系统自动提取，可能不准确；与当前话题相关时可向用户求证，"
            "用户确认后调用 confirm_memory）"
        )
        for m in candidates:
            lines.append(f"- [{m.id}] {KIND_LABELS.get(m.kind, m.kind)}：{m.content}")
    if not lines:
        return ""
    return truncate_to_tokens("\n".join(lines), max_tokens)


memory_service = MemoryService()
