import asyncio
from types import SimpleNamespace

from pydantic import BaseModel, Field

from app.agent.loop import AgentLoop
from app.agent.tools.base import BaseTool, ToolContext, ToolError, ToolExecutor
from app.agent.tools.registry import build_default_tools
from app.agent.types import ToolCall
from app.config import AgentSettings

CTX = ToolContext(user_id="u1", session_id="s1")


class EchoParams(BaseModel):
    text: str = Field(min_length=1)
    times: int = Field(1, ge=1, le=3)


class EchoTool(BaseTool):
    name = "echo"
    description = "echo"
    Params = EchoParams

    async def run(self, params, ctx):
        if params.text == "boom":
            raise ToolError("业务错误")
        if params.text == "crash":
            raise RuntimeError("secret internal detail")
        return {"echo": params.text * params.times, "user": ctx.user_id}


class SlowTool(BaseTool):
    name = "slow"
    description = "slow"
    timeout = 0.05

    async def run(self, params, ctx):
        await asyncio.sleep(1)


async def test_executor_validation_and_errors():
    ex = ToolExecutor([EchoTool(), SlowTool()])
    ok = await ex.execute(ToolCall("1", "echo", {"text": "a", "times": 2}), CTX)
    assert ok.success and ok.data == {"echo": "aa", "user": "u1"}

    # 模型伪造 user_id 会被忽略，身份只来自 ToolContext
    forged = await ex.execute(ToolCall("1", "echo", {"text": "a", "user_id": "other"}), CTX)
    assert forged.data["user"] == "u1"

    bad = await ex.execute(ToolCall("2", "echo", {"times": 9}), CTX)
    assert not bad.success and "参数校验失败" in bad.error

    parse = await ex.execute(ToolCall("3", "echo", {}, parse_error="Expecting value"), CTX)
    assert not parse.success and "JSON" in parse.error

    unknown = await ex.execute(ToolCall("4", "nope", {}), CTX)
    assert not unknown.success

    biz = await ex.execute(ToolCall("5", "echo", {"text": "boom"}), CTX)
    assert biz.error == "业务错误"

    crash = await ex.execute(ToolCall("6", "echo", {"text": "crash"}), CTX)
    assert not crash.success and "secret" not in crash.error

    slow = await ex.execute(ToolCall("7", "slow", {}), CTX)
    assert not slow.success and "超时" in slow.error


def test_default_tool_schemas_are_clean():
    for tool in build_default_tools():
        schema = tool.schema()
        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        text = str(params)
        assert "user_id" not in text, tool.name
        assert "$defs" not in text and "'anyOf'" not in text, tool.name
    search = next(t for t in build_default_tools() if t.name == "search_exercises").schema()
    props = search["function"]["parameters"]["properties"]
    assert props["primary_muscles"]["type"] == "array"
    assert "chest" in props["primary_muscles"]["items"]["enum"]


# ---------------- AgentLoop（伪造流式 LLM） ----------------


def chunk(content=None, tool_calls=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=usage)


def tc(index, id=None, name=None, args=None):
    return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=args))


class FakeLLM:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = []

    async def stream(self, messages, tools=None, **kw):
        self.calls.append({"messages": [dict(m) for m in messages], "kw": kw})
        for c in self.rounds.pop(0):
            yield c


async def collect(loop, messages):
    transcript = []
    events = [e async for e in loop.run(messages, CTX, transcript)]
    return events, transcript


async def test_loop_tool_then_answer():
    llm = FakeLLM(
        [
            [chunk(tool_calls=[tc(0, "c1", "echo", '{"te')]), chunk(tool_calls=[tc(0, args='xt": "hi"}')])],
            [chunk("你好"), chunk("！", usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2))],
        ]
    )
    loop = AgentLoop(llm, ToolExecutor([EchoTool()]), AgentSettings(max_iterations=4))
    events, transcript = await collect(loop, [{"role": "user", "content": "hi"}])

    types = [e.type for e in events]
    assert types == ["tool_call", "tool_result", "content", "content", "done"]
    assert events[0].data["arguments"] == {"text": "hi"}
    assert events[-1].data["status"] == "completed"
    assert [m["role"] for m in transcript] == ["assistant", "tool", "assistant"]
    assert transcript[1]["name"] == "echo" and transcript[-1]["content"] == "你好！"
    # 第二次调用模型时能看到工具结果，且发送给模型的 tool 消息不含 name 字段
    second = llm.calls[1]["messages"]
    assert second[-1]["role"] == "tool" and "name" not in second[-1]


async def test_loop_bad_json_is_reported_back():
    llm = FakeLLM(
        [
            [chunk(tool_calls=[tc(0, "c1", "echo", "{bad json")])],
            [chunk("好的")],
        ]
    )
    loop = AgentLoop(llm, ToolExecutor([EchoTool()]), AgentSettings(max_iterations=4))
    events, _ = await collect(loop, [{"role": "user", "content": "x"}])
    result = next(e for e in events if e.type == "tool_result")
    assert not result.data["success"] and "JSON" in result.data["error"]
    assert events[-1].data["status"] == "completed"


async def test_loop_last_round_forces_answer():
    rounds = [[chunk(tool_calls=[tc(0, f"c{i}", "echo", '{"text": "a"}')])] for i in range(2)]
    rounds.append([chunk("总结回答")])
    llm = FakeLLM(rounds)
    loop = AgentLoop(llm, ToolExecutor([EchoTool()]), AgentSettings(max_iterations=3))
    events, transcript = await collect(loop, [{"role": "user", "content": "x"}])
    assert llm.calls[-1]["kw"].get("tool_choice") == "none"
    assert events[-1].data["status"] == "completed"
    assert transcript[-1]["content"] == "总结回答"


async def test_loop_llm_error_ends_with_done():
    class Broken:
        async def stream(self, messages, tools=None, **kw):
            yield chunk("部分")
            raise RuntimeError("network")

    loop = AgentLoop(Broken(), ToolExecutor([EchoTool()]), AgentSettings())
    events, transcript = await collect(loop, [{"role": "user", "content": "x"}])
    assert [e.type for e in events][-2:] == ["error", "done"]
    assert events[-1].data["status"] == "error"
    assert transcript == [{"role": "assistant", "content": "部分"}]
