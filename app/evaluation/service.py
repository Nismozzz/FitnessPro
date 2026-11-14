"""
RAGAS 在线评估（迁移自 CookHero 的 evaluation_service，按本项目的结构重写）。

流程：一轮对话结束 → 从本轮工具结果中提取检索上下文 → 写入 pending 记录 → 后台打分并回填。
评估是旁路逻辑：任何失败都只记录日志/状态，绝不影响对话。

与 CookHero 的差异：
- 使用 ragas 0.4 的 collections 指标 + llm_factory（直接复用 OpenAI SDK 客户端），
  不再需要 LangChain 和 FilteredChatOpenAI 包装；
- 上下文取自 Agent 工具结果（search_exercises / get_exercise_detail），而不是单独的检索步骤；
- 每轮至多一条记录（唯一约束），并用信号量限制并发评估数；
- ragas 是可选依赖，仅在 evaluation.enabled=true 时才导入。
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import Any, Protocol

from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import EvaluationSettings, settings
from app.database.models import RagEvaluation
from app.database.session import SessionLocal
from app.utils.background import spawn
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

RETRIEVAL_TOOLS = frozenset({"search_exercises", "get_exercise_detail"})
SCORE_NAMES = ("faithfulness", "answer_relevancy")


class Scorer(Protocol):
    async def score(self, query: str, contexts: list[str], response: str) -> dict[str, float | None]: ...


def extract_contexts(transcript: list[dict[str, Any]], max_chars: int) -> list[str]:
    """取本轮检索类工具的结果作为 RAG 上下文；成功与否由 ok 字段粗略判断，失败结果不算上下文。"""
    contexts: list[str] = []
    total = 0
    for m in transcript:
        if m.get("role") != "tool" or m.get("name") not in RETRIEVAL_TOOLS:
            continue
        content = (m.get("content") or "").strip()
        if not content or content.startswith('{"ok": false'):
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        contexts.append(content[:remaining])
        total += len(contexts[-1])
    return contexts


def _clean(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


class RagasScorer:
    """延迟初始化 ragas；首次评估时才导入，未安装时给出明确错误。"""

    def __init__(self, cfg: EvaluationSettings):
        self.cfg = cfg
        self._metrics: dict[str, Any] | None = None
        self._init_lock = asyncio.Lock()

    async def _ensure(self) -> dict[str, Any]:
        if self._metrics is not None:
            return self._metrics
        async with self._init_lock:
            if self._metrics is None:
                self._metrics = await asyncio.to_thread(self._build)
        return self._metrics

    def _build(self) -> dict[str, Any]:
        try:
            from openai import AsyncOpenAI
            from ragas.embeddings.base import embedding_factory
            from ragas.llms import llm_factory
            from ragas.metrics.collections import AnswerRelevancy, Faithfulness
        except ImportError as exc:
            raise RuntimeError("未安装 ragas，请执行 pip install 'ragas>=0.4'") from exc

        profile = getattr(settings.llm, self.cfg.llm_tier)
        client = AsyncOpenAI(
            api_key=profile.api_key or "missing",
            base_url=profile.base_url,
            timeout=profile.timeout,
            max_retries=2,
        )
        extra_body = {**profile.extra_body, **self.cfg.llm_extra_body}
        llm = llm_factory(
            profile.model,
            client=client,
            temperature=0.0,
            max_tokens=min(profile.max_tokens, self.cfg.llm_max_tokens),
            **({"extra_body": extra_body} if extra_body else {}),
        )
        metrics: dict[str, Any] = {}
        if "faithfulness" in self.cfg.metrics:
            metrics["faithfulness"] = Faithfulness(llm=llm)
        if "answer_relevancy" in self.cfg.metrics:
            emb = settings.embedding
            if emb.provider != "openai":
                raise RuntimeError("answer_relevancy 目前只支持 openai 兼容的 embedding.provider")
            emb_client = AsyncOpenAI(
                api_key=emb.api_key or "missing", base_url=emb.base_url, timeout=emb.timeout, max_retries=2
            )
            embeddings = embedding_factory("openai", model=emb.model, client=emb_client, interface="modern")
            metrics["answer_relevancy"] = AnswerRelevancy(llm=llm, embeddings=embeddings)
        logger.info("RAGAS 已初始化，指标：%s", list(metrics))
        return metrics

    async def score(self, query: str, contexts: list[str], response: str) -> dict[str, float | None]:
        metrics = await self._ensure()

        async def one(name: str, metric: Any) -> tuple[str, float | None]:
            started = time.perf_counter()
            try:
                if name == "faithfulness":
                    result = await metric.ascore(
                        user_input=query, response=response, retrieved_contexts=contexts
                    )
                else:
                    result = await metric.ascore(user_input=query, response=response)
                value = _clean(result.value)
            except Exception as exc:  # 单个指标失败不影响其他指标
                logger.warning("指标 %s 评估失败：%s", name, repr(exc), exc_info=True)
                value = None
            logger.info("指标 %s=%s 耗时 %.1fs", name, value, time.perf_counter() - started)
            return name, value

        # ragas 每个指标内部是串行的多次 LLM 调用，指标之间互相独立，并发执行可明显缩短总时长
        return dict(await asyncio.gather(*(one(n, m) for n, m in metrics.items())))


class EvaluationService:
    def __init__(self, cfg: EvaluationSettings | None = None, scorer: Scorer | None = None):
        self.cfg = cfg or settings.evaluation
        self._scorer = scorer
        self._semaphore: asyncio.Semaphore | None = None

    @property
    def scorer(self) -> Scorer:
        if self._scorer is None:
            self._scorer = RagasScorer(self.cfg)
        return self._scorer

    def _should_sample(self) -> bool:
        return self.cfg.sample_rate >= 1.0 or random.random() < self.cfg.sample_rate

    async def schedule(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
        query: str,
        transcript: list[dict[str, Any]],
        response: str,
    ) -> asyncio.Task | None:
        """对一轮回答安排异步评估。不满足条件（未启用/未抽中/无检索上下文/无回答）时返回 None。"""
        if not self.cfg.enabled or not response.strip() or not self._should_sample():
            return None
        contexts = extract_contexts(transcript, self.cfg.max_context_chars)
        if not contexts:
            return None
        try:
            async with SessionLocal() as db:
                record = RagEvaluation(
                    user_id=user_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    query=query,
                    contexts=contexts,
                    response=response,
                )
                db.add(record)
                await db.commit()
        except IntegrityError:
            logger.debug("turn %s 已有评估记录，跳过", turn_id)
            return None
        except Exception:
            logger.exception("创建评估记录失败（不影响对话）")
            return None
        return spawn(self._run(record.id, query, contexts, response), name=f"eval:{record.id}")

    async def _run(self, evaluation_id: str, query: str, contexts: list[str], response: str) -> None:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(max(1, self.cfg.max_concurrency))
        scores: dict[str, float | None] = {}
        status, error = "completed", None
        start = time.perf_counter()
        try:
            async with self._semaphore:
                start = time.perf_counter()  # 不含排队等待
                scores = await asyncio.wait_for(
                    self.scorer.score(query, contexts, response), timeout=self.cfg.timeout_seconds
                )
            if all(scores.get(n) is None for n in self.cfg.metrics):
                status, error = "failed", "所有指标均未得到有效分数"
        except asyncio.TimeoutError:
            status, error = "failed", f"评估超时（{self.cfg.timeout_seconds:g}s）"
        except Exception as exc:
            status, error = "failed", (str(exc) or repr(exc))[:500]
            logger.exception("评估 %s 失败", evaluation_id)

        values = {n: scores.get(n) for n in SCORE_NAMES}
        async with SessionLocal() as db:
            await db.execute(
                update(RagEvaluation)
                .where(RagEvaluation.id == evaluation_id)
                .values(
                    **values,
                    status=status,
                    error_message=error,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    evaluated_at=utcnow(),
                )
            )
            await db.commit()
        self._alert(evaluation_id, values)

    def _alert(self, evaluation_id: str, scores: dict[str, float | None]) -> None:
        thresholds = {
            "faithfulness": self.cfg.alert_faithfulness,
            "answer_relevancy": self.cfg.alert_answer_relevancy,
        }
        for name, threshold in thresholds.items():
            value = scores.get(name)
            if value is not None and value < threshold:
                logger.warning("RAG 质量告警 eval=%s %s=%.2f < %.2f", evaluation_id, name, value, threshold)


def evaluation_to_dict(e: RagEvaluation, *, detail: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": e.id,
        "session_id": e.session_id,
        "turn_id": e.turn_id,
        "query": e.query,
        "faithfulness": e.faithfulness,
        "answer_relevancy": e.answer_relevancy,
        "status": e.status,
        "error_message": e.error_message,
        "duration_ms": e.duration_ms,
        "created_at": e.created_at.isoformat(),
        "evaluated_at": e.evaluated_at.isoformat() if e.evaluated_at else None,
    }
    if detail:
        data["contexts"] = e.contexts
        data["response"] = e.response
    return data


async def list_evaluations(user_id: str, limit: int = 50, session_id: str | None = None) -> list[RagEvaluation]:
    stmt = select(RagEvaluation).where(RagEvaluation.user_id == user_id)
    if session_id:
        stmt = stmt.where(RagEvaluation.session_id == session_id)
    stmt = stmt.order_by(RagEvaluation.created_at.desc()).limit(limit)
    async with SessionLocal() as db:
        return list((await db.execute(stmt)).scalars().all())


async def get_evaluation(user_id: str, evaluation_id: str) -> RagEvaluation | None:
    async with SessionLocal() as db:
        row = await db.get(RagEvaluation, evaluation_id)
        return row if row is not None and row.user_id == user_id else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _metric_stats(rows: list[RagEvaluation], name: str) -> dict[str, float | None]:
    values = [v for r in rows if (v := getattr(r, name)) is not None]
    return {"mean": _mean(values), "min": min(values, default=None), "max": max(values, default=None)}


def violated_thresholds(e: RagEvaluation, cfg: EvaluationSettings) -> list[str]:
    out = []
    if e.faithfulness is not None and e.faithfulness < cfg.alert_faithfulness:
        out.append("faithfulness")
    if e.answer_relevancy is not None and e.answer_relevancy < cfg.alert_answer_relevancy:
        out.append("answer_relevancy")
    return out


async def statistics(user_id: str) -> dict[str, Any]:
    """当前用户的整体统计。分数只统计 completed 记录。"""
    async with SessionLocal() as db:
        rows = list(
            (await db.execute(select(RagEvaluation).where(RagEvaluation.user_id == user_id))).scalars().all()
        )
    done = [r for r in rows if r.status == "completed"]
    durations = [r.duration_ms for r in done if r.duration_ms is not None]
    return {
        "total_evaluations": len(rows),
        "completed_count": len(done),
        "pending_count": sum(r.status == "pending" for r in rows),
        "failed_count": sum(r.status == "failed" for r in rows),
        "metrics": {n: _metric_stats(done, n) for n in SCORE_NAMES},
        "avg_evaluation_duration_ms": _mean(durations),
    }


async def trends(user_id: str, days: int = 7, granularity: str = "day") -> dict[str, Any]:
    """按天/小时聚合平均分（UTC 分桶，period 带 Z 后缀，前端按本地时间展示）。"""
    since = utcnow() - timedelta(days=days)
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(RagEvaluation)
                    .where(
                        RagEvaluation.user_id == user_id,
                        RagEvaluation.status == "completed",
                        RagEvaluation.created_at >= since,
                    )
                    .order_by(RagEvaluation.created_at)
                )
            )
            .scalars()
            .all()
        )
    fmt = "%Y-%m-%dT%H:00:00Z" if granularity == "hour" else "%Y-%m-%dT00:00:00Z"
    buckets: dict[str, list[RagEvaluation]] = {}
    for r in rows:
        buckets.setdefault(r.created_at.strftime(fmt), []).append(r)
    points = [
        {
            "period": period,
            "count": len(items),
            "metrics": {n: _mean([v for r in items if (v := getattr(r, n)) is not None]) for n in SCORE_NAMES},
        }
        for period, items in buckets.items()
    ]
    return {"period_days": days, "granularity": granularity, "data_points": len(points), "trends": points}


async def alerts(user_id: str, limit: int = 20, cfg: EvaluationSettings | None = None) -> dict[str, Any]:
    cfg = cfg or settings.evaluation
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(RagEvaluation)
                .where(
                    RagEvaluation.user_id == user_id,
                    RagEvaluation.status == "completed",
                    or_(
                        RagEvaluation.faithfulness < cfg.alert_faithfulness,
                        RagEvaluation.answer_relevancy < cfg.alert_answer_relevancy,
                    ),
                )
                .order_by(RagEvaluation.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    return {
        "thresholds": {
            "faithfulness": cfg.alert_faithfulness,
            "answer_relevancy": cfg.alert_answer_relevancy,
        },
        "count": len(rows),
        "alerts": [
            {**evaluation_to_dict(r, detail=True), "violated_thresholds": violated_thresholds(r, cfg)}
            for r in rows
        ],
    }


def health(cfg: EvaluationSettings | None = None) -> dict[str, Any]:
    cfg = cfg or settings.evaluation
    return {
        "enabled": cfg.enabled,
        "sample_rate": cfg.sample_rate,
        "configured_metrics": list(cfg.metrics),
        "timeout_seconds": cfg.timeout_seconds,
        "alert_thresholds": {
            "faithfulness": cfg.alert_faithfulness,
            "answer_relevancy": cfg.alert_answer_relevancy,
        },
    }


evaluation_service = EvaluationService()
