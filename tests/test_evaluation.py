import asyncio
import json

from app.config import EvaluationSettings
from app.database.models import RagEvaluation, User
from app.database.session import SessionLocal
from app.evaluation import service as eval_svc
from app.evaluation.service import EvaluationService, extract_contexts


def tool_msg(name, payload):
    return {"role": "tool", "name": name, "content": json.dumps(payload, ensure_ascii=False)}


TRANSCRIPT = [
    {"role": "user", "content": "练胸"},
    tool_msg("get_current_time", {"ok": True, "data": "now"}),
    tool_msg("search_exercises", {"ok": True, "data": [{"name": "卧推"}]}),
    tool_msg("get_exercise_detail", {"ok": False, "error": "not found"}),
]


class FakeScorer:
    def __init__(self, scores=None, exc=None, delay=0.0):
        self.scores, self.exc, self.delay = scores, exc, delay

    async def score(self, query, contexts, response):
        await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.scores


def test_extract_contexts_keeps_only_successful_retrieval_results():
    contexts = extract_contexts(TRANSCRIPT, 1000)
    assert len(contexts) == 1 and "卧推" in contexts[0]


def test_extract_contexts_respects_char_budget():
    contexts = extract_contexts([tool_msg("search_exercises", {"ok": True, "data": "x" * 500})] * 3, 600)
    assert sum(map(len, contexts)) == 600


async def _schedule(svc, turn_id="t1", transcript=TRANSCRIPT, response="推荐卧推"):
    async with SessionLocal() as db:
        if await db.get(User, "u1") is None:
            db.add(User(id="u1", username="eval-user", password_hash="x"))
            await db.commit()
    task = await svc.schedule(
        user_id="u1", session_id="s1", turn_id=turn_id, query="练胸", transcript=transcript, response=response
    )
    if task:
        await task
    return task


async def _row(turn_id="t1"):
    async with SessionLocal() as db:
        from sqlalchemy import select

        return (await db.execute(select(RagEvaluation).where(RagEvaluation.turn_id == turn_id))).scalar_one()


async def test_disabled_or_no_context_is_skipped(db):
    assert await _schedule(EvaluationService(EvaluationSettings(enabled=False), FakeScorer({}))) is None
    svc = EvaluationService(EvaluationSettings(enabled=True), FakeScorer({}))
    assert await _schedule(svc, "t2", transcript=[tool_msg("get_current_time", {"ok": True})]) is None
    assert await _schedule(svc, "t3", response="  ") is None


async def test_completed_evaluation_is_persisted(db):
    svc = EvaluationService(
        EvaluationSettings(enabled=True), FakeScorer({"faithfulness": 0.9, "answer_relevancy": 0.8})
    )
    await _schedule(svc, "t4")
    row = await _row("t4")
    assert (row.status, row.faithfulness, row.answer_relevancy) == ("completed", 0.9, 0.8)
    assert row.duration_ms is not None and row.evaluated_at is not None
    assert (await eval_svc.statistics("u1"))["completed_count"] >= 1


async def test_duplicate_turn_is_ignored(db):
    svc = EvaluationService(EvaluationSettings(enabled=True), FakeScorer({"faithfulness": 1.0}))
    assert await _schedule(svc, "t5") is not None
    assert await _schedule(svc, "t5") is None


async def test_scorer_failure_and_timeout_are_recorded_not_raised(db):
    svc = EvaluationService(EvaluationSettings(enabled=True), FakeScorer(exc=RuntimeError("boom")))
    await _schedule(svc, "t6")
    row = await _row("t6")
    assert row.status == "failed" and "boom" in row.error_message

    svc = EvaluationService(
        EvaluationSettings(enabled=True, timeout_seconds=0.05), FakeScorer({"faithfulness": 1.0}, delay=1)
    )
    await _schedule(svc, "t7")
    assert (await _row("t7")).status == "failed"


async def test_all_none_scores_mark_failed(db):
    svc = EvaluationService(EvaluationSettings(enabled=True), FakeScorer({"faithfulness": None}))
    await _schedule(svc, "t8")
    assert (await _row("t8")).status == "failed"


async def test_statistics_trends_and_alerts(db):
    svc = EvaluationService(
        EvaluationSettings(enabled=True), FakeScorer({"faithfulness": 0.1, "answer_relevancy": 0.9})
    )
    await _schedule(svc, "t9")
    stats = await eval_svc.statistics("u1")
    assert stats["metrics"]["faithfulness"]["min"] <= 0.1
    assert stats["total_evaluations"] == stats["completed_count"] + stats["pending_count"] + stats["failed_count"]
    day = await eval_svc.trends("u1", 7, "day")
    hour = await eval_svc.trends("u1", 7, "hour")
    assert day["data_points"] >= 1 and day["trends"][0]["period"].endswith("T00:00:00Z")
    assert hour["trends"][0]["period"].endswith(":00:00Z")
    result = await eval_svc.alerts("u1")
    assert any(a["turn_id"] == "t9" and a["violated_thresholds"] == ["faithfulness"] for a in result["alerts"])
    empty = await eval_svc.alerts("nobody")
    assert empty["count"] == 0 and empty["alerts"] == []
    assert eval_svc.health()["alert_thresholds"]["faithfulness"] == 0.3
