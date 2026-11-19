import pytest

from app.agent import skills
from app.agent.loop import AgentLoop
from app.agent.tools.base import BaseTool, ToolExecutor
from app.agent.tools.skill_tools import LoadTrainingPlanSkillTool
from app.config import AgentSettings
from tests.test_agent import FakeLLM, chunk, collect, tc


HEADER = "---\nname: training-plan-design\ndescription: 训练规划测试\n---\n"


@pytest.fixture(autouse=True)
def disable_telemetry_in_unit_tests(monkeypatch):
    monkeypatch.setattr("app.observability.recorder.record", lambda *args, **kwargs: None)


def test_bundled_skill_loads_independently_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = skills.load_training_plan_skill()
    assert body.startswith("# 训练计划编排标准")
    assert "## 重量日与容量日" in body
    assert "## 渐进超负荷与周期化" in body
    assert not body.startswith("---")
    metadata = skills.load_training_plan_skill_metadata()
    assert "training-plan-design" in metadata
    assert "## 重量日与容量日" not in metadata
    assert body not in metadata


def test_missing_skill_fails_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "TRAINING_PLAN_SKILL_PATH", tmp_path / "missing.md")
    with pytest.raises(RuntimeError, match="无法读取训练规划技能"):
        skills.load_training_plan_skill()


@pytest.mark.parametrize("content", [
    "# 缺少元信息",
    "---\nname: training-plan-design\n",
    "---\nname: [\n---\n正文",
    "---\nname: other\ndescription: test\n---\n正文",
    "---\nname: training-plan-design\n---\n正文",
    HEADER + "\n",
])
def test_invalid_skill_is_not_silently_ignored(content, tmp_path, monkeypatch):
    path = tmp_path / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(skills, "TRAINING_PLAN_SKILL_PATH", path)
    with pytest.raises(ValueError, match="训练规划技能"):
        skills.load_training_plan_skill()


async def test_skill_full_body_is_not_truncated_persisted_or_duplicated(tmp_path, monkeypatch):
    body = "完整规则" * 2000 + "\n末尾规则 {sets}"
    path = tmp_path / "SKILL.md"
    path.write_text(HEADER + body, encoding="utf-8")
    monkeypatch.setattr(skills, "TRAINING_PLAN_SKILL_PATH", path)
    llm = FakeLLM([
        [chunk(tool_calls=[tc(0, "load1", "load_training_plan_skill", "{}")])],
        [chunk(tool_calls=[tc(0, "load2", "load_training_plan_skill", "{}")])],
        [chunk("拟定安排")],
    ])
    events, transcript = await collect(AgentLoop(
        llm, ToolExecutor([LoadTrainingPlanSkillTool()]),
        AgentSettings(max_iterations=4), tool_result_max_tokens=50,
    ), [{"role": "user", "content": "帮我规划一周训练"}])
    for call in llm.calls[1:]:
        system = [m for m in call["messages"] if m["role"] == "system"]
        assert len(system) == 1 and body in system[0]["content"]
    assert not any(body in (m.get("content") or "") for m in transcript)
    results = [e.data for e in events if e.type == "tool_result"]
    assert all(r["data"] == {"name": "training-plan-design", "loaded": True} for r in results)
    assert events[-1].data["status"] == "completed"


@pytest.mark.parametrize("mode", ["not_requested", "disabled", "failed"])
async def test_skill_is_not_injected_without_successful_load(mode, tmp_path, monkeypatch):
    body = skills.load_training_plan_skill()
    tools = [] if mode == "disabled" else [LoadTrainingPlanSkillTool()]
    rounds = []
    if mode != "not_requested":
        rounds.append([chunk(tool_calls=[tc(0, "load", "load_training_plan_skill", "{}")])])
    if mode == "failed":
        monkeypatch.setattr(skills, "TRAINING_PLAN_SKILL_PATH", tmp_path / "missing.md")
    rounds.append([chunk("说明或收集条件")])
    llm = FakeLLM(rounds)
    events, _ = await collect(AgentLoop(
        llm, ToolExecutor(tools), AgentSettings(max_iterations=3),
    ), [{"role": "user", "content": "训练问题"}])
    assert not any(
        body in (message.get("content") or "")
        for call in llm.calls for message in call["messages"]
    )
    results = [e.data for e in events if e.type == "tool_result"]
    assert all(not r["success"] for r in results)


async def test_plan_tool_requires_skill_visible_in_previous_model_request():
    class DraftTool(BaseTool):
        name = "draft_weekly_plan"
        description = "测试编排"
        runs = 0

        async def run(self, params, ctx):
            self.runs += 1
            return {"valid": True}

    draft = DraftTool()
    llm = FakeLLM([
        [chunk(tool_calls=[tc(0, "early", "draft_weekly_plan", "{}")])],
        [chunk(tool_calls=[
            tc(0, "load", "load_training_plan_skill", "{}"),
            tc(1, "same_batch", "draft_weekly_plan", "{}"),
        ])],
        [chunk(tool_calls=[tc(0, "ready", "draft_weekly_plan", "{}")])],
        [chunk("拟定安排")],
    ])
    events, _ = await collect(AgentLoop(
        llm, ToolExecutor([LoadTrainingPlanSkillTool(), draft]), AgentSettings(max_iterations=5),
    ), [{"role": "user", "content": "帮我制定训练计划"}])
    results = [e.data for e in events if e.type == "tool_result" and e.data["name"] == "draft_weekly_plan"]
    assert [r["success"] for r in results] == [False, False, True]
    assert draft.runs == 1
    # 同批工具调用的结果全部完成后，才注入系统规则。
    roles = [m["role"] for m in llm.calls[2]["messages"]]
    assert roles[-4:] == ["assistant", "tool", "tool", "system"]
