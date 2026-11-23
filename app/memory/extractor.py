"""
从对话中自动提取候选记忆。

只产生 candidate，不直接改写已确认的记忆：如果新信息否定了旧记忆，生成一条
supersedes 旧记忆的候选，用户确认后旧记忆才会归档。区分长期事实与临时状态。
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.llm.provider import get_llm
from app.observability.context import traced_module
from app.llm.tokens import truncate_to_tokens
from app.memory.service import CANDIDATE, MemoryKind, MemoryScope, memory_service
from app.users.profile import DIETARY_FIELDS, get_profile

logger = logging.getLogger(__name__)

EXTRACT_SYSTEM = """你负责从健身和饮食对话中提取关于用户本人、值得在以后的对话中记住的事实。
只提取用户自己明确陈述的信息；不要提取助手的建议，不要推测，不要提取一次性的问题本身。
kind 可选值：
- injury：伤病、疼痛、身体限制（如“左膝半月板受过伤”）
- goal：训练目标（如“明年三月前减脂 5 公斤”）
- preference：对动作或训练方式的喜好与厌恶（如“不喜欢跑步”）
- equipment：可用器械与场地（如“家里只有一对可调哑铃”）
- schedule：可训练的时间安排（如“工作日只能晚上练 45 分钟”）
- body：身体数据（身高、体重、体脂等）
- dietary_preference：用户喜欢或不喜欢的口味、菜式、食材（如“用户不喜欢香菜”）
- dietary_habit：经常重复的用餐和备餐方式（如“用户工作日午餐常点外卖”）
- dietary_restriction：用户明确表示必须遵守的忌口、过敏或饮食方式（如“用户对花生过敏”）
- other：其他与训练或饮食规划相关的重要信息
不要把一次用餐、“今天想吃什么”、一次推荐请求或助手拟定的菜单当成偏好或习惯。不要从不喜欢推断过敏。
scope：长期有效用 long_term；明确只在短期内有效的安排或条件（如“这周出差，只能在外面吃”）用 temporary。
如果新信息更新或否定了某条已有记忆，op 用 "supersede" 并填写 supersedes_id。短期变化与长期信息并存时用 "add"。
已有记忆或饮食画像已包含的信息不要重复提取。饮食画像没有记忆 ID，不能作为 supersedes 的目标。
没有值得记住的信息时返回空数组。只输出 JSON：
{"operations": [{"op": "add", "kind": "injury", "content": "用户左膝有旧伤，深蹲时膝盖会痛", "scope": "long_term", "supersedes_id": null, "confidence": 0.9}]}"""


class ExtractedOp(BaseModel):
    op: Literal["add", "supersede"] = "add"
    kind: MemoryKind
    content: str = Field(min_length=2, max_length=200)
    scope: MemoryScope = "long_term"
    supersedes_id: str | None = None
    confidence: float = Field(0.5, ge=0, le=1)


@traced_module("memory")
async def extract_memories(
    *, user_id: str, session_id: str, user_message: str, assistant_message: str
) -> int:
    """返回新建的候选记忆数量。"""
    active, candidates = await memory_service.for_prompt(user_id)
    existing = [
        {"id": m.id, "kind": m.kind, "content": m.content, "status": m.status, "scope": m.scope}
        for m in active + candidates
    ]
    profile = await get_profile(user_id)
    dietary_profile = {field: profile[field] for field in DIETARY_FIELDS if profile.get(field)}
    prompt = (
        f"已有记忆：\n{json.dumps(existing, ensure_ascii=False)}\n\n"
        f"已保存的饮食画像：\n{json.dumps(dietary_profile, ensure_ascii=False)}\n\n"
        f"本轮对话：\n[用户] {truncate_to_tokens(user_message, 1500)}\n"
        f"[助手] {truncate_to_tokens(assistant_message, 1000)}"
    )
    try:
        data = await get_llm("fast").complete_json(
            [
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": prompt},
            ]
        )
    except Exception as exc:
        logger.warning("记忆提取失败：%s", exc)
        return 0

    existing_ids = {m["id"] for m in existing}
    created = 0
    for raw in (data.get("operations") or [])[:5] if isinstance(data, dict) else []:
        try:
            op = ExtractedOp.model_validate(raw)
        except ValidationError:
            continue
        if op.confidence < 0.6:
            continue
        supersedes = op.supersedes_id if op.op == "supersede" and op.supersedes_id in existing_ids else None
        _, is_new = await memory_service.add(
            user_id,
            kind=op.kind,
            content=op.content,
            scope=op.scope,
            status=CANDIDATE,
            source="auto_extracted",
            source_session_id=session_id,
            supersedes_id=supersedes,
        )
        created += int(is_new)
    if created:
        logger.info("会话 %s 新增 %d 条候选记忆", session_id, created)
    return created
