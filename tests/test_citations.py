import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agent.citations import EvidenceRegistry, history_citations
from app.agent.loop import AgentLoop
from app.agent.tools.base import BaseTool, ToolContext, ToolExecutor
from app.api.agent import list_messages
from app.config import AgentSettings
from app.conversation import repository as repo
from app.database.models import User, new_id
from app.database.session import SessionLocal
from app.llm.tokens import estimate_tokens

DATA = {"exercises": [{"id": "bench", "name": "Bench Press", "name_zh": "卧推",
    "equipment": "杠铃", "secondary_muscles": [], "level": "未标注",
    "instructions": ["第一步原文", "第二步原文"], "instructions_language": "zh"}]}


def test_evidence_tracks_fields_steps_and_rejects_other_turns():
    registry = EvidenceRegistry()
    visible = json.loads(registry.tool_content(DATA, 2500))
    evidence = visible["data"]["evidence"]
    assert all(e["field"] not in {"level", "secondary_muscles"} for e in evidence)
    step = next(e for e in evidence if e["field"] == "instructions_zh[1]")
    assert step["text"] == "第二步原文" and step["language"] == "zh"
    answer = f'步骤[{step["id"]}]，伪造[E999]。'
    clean, used, invalid = registry.resolve(answer)
    assert used == [step] and invalid == ["E999"] and "E999" not in clean
    assert EvidenceRegistry().resolve(answer)[1] == []


def test_budget_preserves_valid_json_and_only_complete_visible_evidence():
    registry = EvidenceRegistry()
    data = {"exercises": [{**DATA["exercises"][0], "instructions": ["超长原文" * 1000, "不能跳过上一步"]}]}
    content = registry.tool_content(data, 500)
    payload = json.loads(content)["data"]
    assert estimate_tokens(content) <= 500
    assert payload["evidence"]
    assert all(not e["field"].startswith("instructions") for e in payload["evidence"])
    assert registry.evidence == {e["id"]: e for e in payload["evidence"]}
    for evidence in payload["evidence"]:
        doc = next(d for d in payload["exercises"] if d["id"] == evidence["exercise_id"])
        assert evidence["field"] in doc
    assert json.loads(EvidenceRegistry().tool_content(DATA, 1))["data"]["evidence"] == []


def chunk(text=None, calls=None):
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=calls))])


class KnowledgeTool(BaseTool):
    name = "get_exercise_detail"
    description = "test knowledge"

    async def run(self, params, ctx):
        return DATA


class CitationLLM:
    def __init__(self, fail=False):
        self.round = 0
        self.fail = fail

    async def stream(self, messages, **kwargs):
        self.round += 1
        if self.round == 1:
            yield chunk(calls=[SimpleNamespace(index=0, id="c1", function=SimpleNamespace(name="get_exercise_detail", arguments="{}"))])
        else:
            data = json.loads(messages[-1]["content"])["data"]
            source = next(e for e in data["evidence"] if e["field"] == "equipment")
            yield chunk(f'需要杠铃[{source["id"]}]。伪造[Eunknown]')
            if self.fail:
                raise RuntimeError("stream lost")


@pytest.mark.parametrize("fail", [False, True])
async def test_loop_cleans_invalid_references_and_keeps_partial_sources(fail):
    transcript = []
    events = [e async for e in AgentLoop(CitationLLM(fail), ToolExecutor([KnowledgeTool()]), AgentSettings()).run(
        [{"role": "user", "content": "器械？"}], ToolContext("u", "s"), transcript)]
    citations = next(e.data["citations"] for e in events if e.type == "citations")
    assert len(citations) == 1 and citations[0]["field"] == "equipment"
    assert "Eunknown" not in transcript[-1]["content"]
    visible = ""
    for event in events:
        if event.type == "content_reset":
            visible = ""
        elif event.type == "content":
            visible += event.data["delta"]
    assert visible == transcript[-1]["content"]
    assert sum(e.type == "done" for e in events) == 1
    assert events[-1].data["status"] == ("error" if fail else "completed")


async def test_snapshot_roundtrip_and_history_ownership(db):
    async with SessionLocal() as session:
        user = User(username="citation_" + new_id(), password_hash="x")
        session.add(user)
        await session.commit()
        user_id = user.id
    session = await repo.create_session(user_id, "引用测试")
    registry = EvidenceRegistry()
    content = registry.tool_content(DATA, 2500)
    source = next(e for e in registry.evidence.values() if e["field"] == "equipment")
    answer = f'使用杠铃[{source["id"]}]'
    await repo.append_messages(session.id, "turn1", [
        {"role": "tool", "name": "get_exercise_detail", "tool_call_id": "c1", "content": content},
        {"role": "assistant", "content": answer},
    ])
    await repo.append_messages(session.id, "turn2", [{"role": "assistant", "content": answer}])
    restored = await list_messages(session.id, user_id)
    assert restored["messages"][1]["citations"] == [source]
    assert restored["messages"][2]["citations"] == []  # 不能借用上一轮证据
    assert restored["messages"][1]["citations"][0]["text"] == "杠铃"
    with pytest.raises(HTTPException) as exc:
        await list_messages(session.id, "other-user")
    assert exc.value.status_code == 404


def test_failed_and_malformed_tool_results_are_not_sources():
    row = SimpleNamespace(turn_id="t", role="tool", name="get_exercise_detail", content='{"ok":false,"data":{"evidence":[]}}')
    bad = SimpleNamespace(turn_id="t", role="tool", name="get_exercise_detail", content='{broken')
    answer = SimpleNamespace(turn_id="t", role="assistant", tool_calls=None, seq=3, content="无来源[E1]")
    assert history_citations([row, bad, answer]) == {3: []}


def test_budget_covers_candidate_names_before_optional_details():
    registry = EvidenceRegistry()
    data = {"exercises": [{**DATA["exercises"][0], "id": f"action_{i}"} for i in range(6)]}
    payload = json.loads(registry.tool_content(data, 2500))["data"]
    assert len(payload["exercises"]) == 6
    assert all(doc.get("name") == "Bench Press" for doc in payload["exercises"])
    assert len({e["id"] for e in payload["evidence"]}) == len(payload["evidence"])


def test_detail_keeps_images_and_aliases_when_budget_allows():
    registry = EvidenceRegistry()
    data = {"exercises": [{**DATA["exercises"][0], "aliases_zh": ["杠铃卧推"], "images": ["https://example.com/bench.jpg"]}]}
    payload = json.loads(registry.tool_content(data, 2500))["data"]
    assert payload["exercises"][0]["images"] == data["exercises"][0]["images"]
    assert next(e for e in payload["evidence"] if e["field"] == "aliases_zh")["text"] == "杠铃卧推"
