import asyncio
import json
import os
import sys

from app import main
from app.agent import service
from app.config import settings
from app.conversation import repository as repo
from app.database.models import new_id
from tests.test_agent import chunk
from tests.test_training import client_for, training  # noqa: F401


async def test_retry_after_process_exits_following_workout_commit(training, monkeypatch):
    user_id, other_user, exercises = training
    question = "记录我今天完成的一组训练"
    body = {"message": question, "model_tier": "fast", "selected_tools": ["log_workout"]}
    log = {
        "idempotency_key": "original-model-key",
        "performed_at": "2026-11-16T19:00:00+08:00",
        "duration_min": 30,
        "sets": [{"exercise_id": exercises[0], "set_no": 1, "reps": 8}],
    }
    turn_id = new_id()
    script = """
import asyncio
import json
import os
import sys

from app.agent.tools.base import ToolContext, ToolExecutor
from app.agent.tools.plan_tools import LogWorkoutTool
from app.agent.types import ToolCall
from app.conversation.repository import create_turn

async def run():
    user, turn_id, body, log = json.loads(sys.argv[1])
    turn = await create_turn(user, None, body["message"], turn_id, body, body["message"], turn_id)
    ctx = ToolContext(user, turn.session_id, turn_id=turn.id)
    executor = ToolExecutor([LogWorkoutTool()])
    first = await executor.execute(ToolCall("a", "log_workout", log), ctx)
    second = await executor.execute(ToolCall("b", "log_workout", {**log, "idempotency_key": "regenerated-key"}), ctx)
    assert first.success and second.success and first.data["id"] == second.data["id"]
    os._exit(0)

asyncio.run(run())
"""
    env = {key: os.environ[key] for key in ("DATABASE_URL", "JWT_SECRET")}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, json.dumps([user_id, turn_id, body, log]),
        env=env, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, errors = await asyncio.wait_for(process.communicate(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, errors.decode()

    path = f"/api/v1/agent/turns/{turn_id}"
    async with await client_for(user_id) as client, await client_for(other_user) as other:
        running = (await client.get(path)).json()
        assert running["status"] == "running"
        assert running["committed_tools"] == ["log_workout"]
        assert (await other.get(path)).status_code == 404
        assert (await other.post(f"{path}/retry")).status_code == 404
        assert (await client.post(f"{path}/retry")).status_code == 409
        logs = (await client.get("/api/v1/logs")).json()
        assert len(logs) == 1
        assert running["committed_results"][0]["result"]["id"] == logs[0]["id"]

        async def noop():
            pass

        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.setattr(main, "init_retriever", noop)
        monkeypatch.setattr(main, "init_article_retriever", lambda: None)
        monkeypatch.setattr(main, "init_meal_retriever", lambda: None)
        monkeypatch.setattr(settings.memory, "auto_extract", False)

        class Model:
            async def stream(self, messages, tools=None, **kwargs):
                assert tools is None
                assert logs[0]["id"] in messages[0]["content"]
                assert [m["content"] for m in messages if m["role"] == "user"] == [question]
                yield chunk("训练已保存。")

        def get_llm(tier):
            assert tier == "fast"
            return Model()

        monkeypatch.setattr(service, "get_llm", get_llm)
        async with main.lifespan(main.app):
            assert (await client.get(path)).json()["status"] == "interrupted"
            retry = await client.post(f"{path}/retry")
            assert retry.status_code == 200 and "训练已保存。" in retry.text
            assert retry.headers["x-request-id"] != turn_id
            result = (await client.get(path)).json()
            assert (result["status"], result["answer"]) == ("completed", "训练已保存。")
            messages = await repo.load_messages(result["session_id"])
            assert [(m.turn_id, m.role) for m in messages] == [(turn_id, "user"), (turn_id, "assistant")]
            assert len((await client.get("/api/v1/logs")).json()) == 1
            assert (await client.post(f"{path}/retry")).status_code == 409
