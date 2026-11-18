"""集成测试：真实 SQLite + 伪造 LLM，覆盖记忆、会话持久化、上下文压缩。"""

from types import SimpleNamespace

import pytest

from app.agent import service as agent_service_module
from app.agent.service import AgentService, SessionNotFound
from app.config import settings
from app.context import compressor as compressor_module
from app.context.builder import ContextBuilder
from app.conversation import repository as repo
from app.database.models import User, new_id
from app.database.session import SessionLocal
from app.memory.service import ACTIVE, ARCHIVED, CANDIDATE, MemoryNotFound, memory_service
from app.users.profile import ProfileData, get_profile, update_profile


async def make_user() -> str:
    async with SessionLocal() as s:
        user = User(username="u_" + new_id()[:8], password_hash="x")
        s.add(user)
        await s.commit()
        return user.id


def chunk(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=None))], usage=None
    )


class ScriptedLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []

    async def stream(self, messages, tools=None, **kw):
        self.seen.append([dict(m) for m in messages])
        yield chunk(self.answers.pop(0))


class FakeFast:
    def __init__(self):
        self.calls = 0

    async def complete_text(self, messages, **kw):
        self.calls += 1
        return f"摘要#{self.calls}：用户想增肌"


async def run_turn(service, user_id, session_id, message):
    sid = await service.prepare_session(user_id, session_id, message)
    queue = service.start_turn(user_id=user_id, session_id=sid, message=message)
    events = []
    while (e := await queue.get()) is not None:
        events.append(e)
    return sid, events


async def test_memory_lifecycle_and_ownership(db):
    alice, bob = await make_user(), await make_user()
    old, _ = await memory_service.add(alice, kind="injury", content="左膝有旧伤", status=ACTIVE, source="manual")
    new, created = await memory_service.add(
        alice, kind="injury", content="左膝已经康复", status=CANDIDATE, source="auto_extracted",
        supersedes_id=old.id,
    )
    assert created
    # 重复内容不新建
    _, dup = await memory_service.add(alice, kind="injury", content="左膝已经康复。", source="auto_extracted")
    assert not dup

    with pytest.raises(MemoryNotFound):
        await memory_service.confirm(bob, new.id)

    await memory_service.confirm(alice, new.id)
    statuses = {m.id: m.status for m in await memory_service.list(alice)}
    assert statuses[new.id] == ACTIVE and statuses[old.id] == ARCHIVED

    temp, _ = await memory_service.add(alice, kind="schedule", content="这周出差", scope="temporary", status=ACTIVE, source="manual")
    assert temp.expires_at is not None
    active, candidates = await memory_service.for_prompt(alice)
    assert {m.id for m in active} == {new.id, temp.id} and candidates == []


async def test_profile_update_merges(db):
    uid = await make_user()
    await update_profile(uid, ProfileData(goal="增肌", equipment=["哑铃"]))
    await update_profile(uid, ProfileData(training_days_per_week=3))
    p = await get_profile(uid)
    assert p["goal"] == "muscle_gain" and p["equipment"] == ["dumbbell"] and p["training_days_per_week"] == 3


async def test_turns_persist_and_session_ownership(db, monkeypatch):
    monkeypatch.setattr(settings.memory, "auto_extract", False)
    llm = ScriptedLLM(["第一轮回答", "第二轮回答"])
    monkeypatch.setattr(agent_service_module, "get_llm", lambda tier="normal": llm)
    service = AgentService()
    uid, other = await make_user(), await make_user()

    sid, events = await run_turn(service, uid, None, "我想增肌")
    assert events[0].type == "session" and events[-1].type == "done"
    assert events[-1].data["status"] == "completed"

    await run_turn(service, uid, sid, "那练几天？")
    # 第二轮模型输入包含第一轮历史
    contents = [m.get("content") for m in llm.seen[1]]
    assert "我想增肌" in contents and "第一轮回答" in contents

    rows = await repo.load_messages(sid)
    assert [r.seq for r in rows] == [1, 2, 3, 4]
    assert [r.role for r in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[0].turn_id == rows[1].turn_id != rows[2].turn_id

    with pytest.raises(SessionNotFound):
        await service.prepare_session(other, sid, "偷看")


async def test_compression_keeps_history_contiguous(db, monkeypatch):
    fast = FakeFast()
    monkeypatch.setattr(compressor_module, "get_llm", lambda tier="fast": fast)
    monkeypatch.setattr(settings.context, "history_token_budget", 300)
    monkeypatch.setattr(settings.context, "compress_target_ratio", 0.5)
    monkeypatch.setattr(settings.context, "min_recent_turns", 1)

    uid = await make_user()
    session = await repo.create_session(uid, "t")
    for i in range(6):
        await repo.append_messages(
            session.id, f"turn{i}",
            [{"role": "user", "content": f"问题{i}" + "啊" * 40}, {"role": "assistant", "content": f"回答{i}" + "嗯" * 40}],
        )

    built = await ContextBuilder().build(session_id=session.id, user_id=uid, user_message="新问题")
    assert built.stats["compressed_now"] and built.stats["has_summary"]
    assert fast.calls == 1

    refreshed = await repo.get_session(session.id)
    assert refreshed.version == 1 and refreshed.summary.startswith("摘要#1")
    # 摘要覆盖到 summary_upto_seq 之前，之后的消息全部以原文出现：无缺口
    remaining = await repo.load_messages(session.id, refreshed.summary_upto_seq)
    history_contents = [m["content"] for m in built.messages[1:-1]]
    assert [r.content for r in remaining] == history_contents
    assert built.stats["dropped_turns"] == 0
    assert "摘要#1" in built.messages[0]["content"]

    # 过期版本号的写入会被拒绝
    assert not await repo.update_summary(session.id, 0, "旧版本", 1)


async def test_context_only_loads_skill_metadata_and_refreshes_next_turn(db, tmp_path, monkeypatch):
    from app.agent import skills
    from app.llm.tokens import message_tokens

    uid = await make_user()
    await update_profile(uid, ProfileData(goal="muscle_gain"))
    session = await repo.create_session(uid, "训练计划")
    await repo.append_messages(session.id, "previous", [
        {"role": "user", "content": "每周练四天"},
        {"role": "assistant", "content": "已了解可用天数"},
    ])
    builder = ContextBuilder()
    built = await builder.build(session_id=session.id, user_id=uid, user_message="请制定计划")
    body = skills.load_training_plan_skill()
    assert built.messages[0]["role"] == "system"
    assert body not in built.messages[0]["content"]
    assert skills.load_training_plan_skill_metadata() in built.messages[0]["content"]
    assert "目标：增肌" in built.messages[0]["content"]
    assert built.messages[1]["content"] == "每周练四天"
    assert built.messages[-1] == {"role": "user", "content": "请制定计划"}
    assert built.stats["system_tokens"] == message_tokens(built.messages[0])

    path = tmp_path / "SKILL.md"
    header = "---\nname: training-plan-design\ndescription: 测试\n---\n"
    monkeypatch.setattr(skills, "TRAINING_PLAN_SKILL_PATH", path)
    for description, rule in (("第一版描述", "第一版规则 {sets}"), ("第二版描述", "第二版规则 {reps}")):
        path.write_text(header.replace("测试", description) + rule, encoding="utf-8")
        refreshed = await builder.build(session_id=session.id, user_id=uid, user_message="调整计划")
        assert description in refreshed.messages[0]["content"]
        assert rule not in refreshed.messages[0]["content"]
        assert body not in refreshed.messages[0]["content"]
        assert "description: 测试" not in refreshed.messages[0]["content"]


async def test_skill_body_is_only_visible_during_planning_turn(db, monkeypatch):
    from app.agent import skills
    from app.utils.background import drain
    from tests.test_agent import chunk as tool_chunk, tc

    body = skills.load_training_plan_skill()

    async def no_compression(*args):
        return False

    class Model:
        def __init__(self):
            self.seen = []

        async def stream(self, messages, **kwargs):
            self.seen.append([dict(m) for m in messages])
            if len(self.seen) == 1:
                yield tool_chunk(tool_calls=[tc(0, "skill", "load_training_plan_skill", "{}")])
            else:
                yield chunk("请补充训练条件" if len(self.seen) == 2 else "原理说明")

    llm = Model()
    monkeypatch.setattr(agent_service_module, "get_llm", lambda tier: llm)
    monkeypatch.setattr(settings.memory, "auto_extract", False)
    monkeypatch.setattr(settings.evaluation, "enabled", False)
    monkeypatch.setattr(agent_service_module.context_compressor, "maybe_compress", no_compression)
    uid = await make_user()
    service = AgentService()
    sid, events = await run_turn(service, uid, None, "帮我制定训练计划")
    assert events[-1].data["status"] == "completed"
    assert not any(body in (m.get("content") or "") for m in llm.seen[0])
    assert any(m["role"] == "system" and body in m["content"] for m in llm.seen[1])
    rows = await repo.load_messages(sid)
    assert not any(body in (r.content or "") for r in rows)

    await run_turn(service, uid, sid, "先不做计划，解释肌肥大原理")
    assert not any(body in (m.get("content") or "") for m in llm.seen[2])
    assert skills.load_training_plan_skill_metadata() in llm.seen[2][0]["content"]
    await drain()
