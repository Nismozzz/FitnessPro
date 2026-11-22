from collections import Counter, defaultdict
from datetime import timedelta
import json
import logging

from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import resolve_path, settings
from app.database.models import LLMCall, RetrievalLog, ToolCallLog
from app.database.session import SessionLocal
from app.utils.time import utcnow

logger = logging.getLogger(__name__)
TABLES = {"llm_calls": LLMCall, "tool_calls": ToolCallLog, "retrieval_logs": RetrievalLog}


async def get_today_tokens(user_id: str) -> int:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    tokens = func.coalesce(LLMCall.prompt_tokens, 0) + func.coalesce(LLMCall.completion_tokens, 0)
    query = select(func.coalesce(func.sum(tokens), 0)).where(
        LLMCall.user_id == user_id,
        LLMCall.created_at >= start,
        LLMCall.created_at < start + timedelta(days=1),
    )
    async with SessionLocal() as db:
        return int(await db.scalar(query))


def access(user_id, scope):
    admin = user_id in settings.observability.admin_user_ids
    if scope == "all" and not admin:
        raise HTTPException(403, "仅管理员可查看全局运行数据")
    return admin


def percentile(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    pos = (len(values) - 1) * p
    i = int(pos)
    return round(values[i] + (values[min(i + 1, len(values) - 1)] - values[i]) * (pos - i), 2)


def latency(rows, field="latency_ms"):
    values = [getattr(r, field) for r in rows if getattr(r, field) is not None]
    return {"count": len(values), "mean": round(sum(values) / len(values), 2) if values else None,
            "p50": percentile(values, .5), "p95": percentile(values, .95)}


async def load_rows(model, user_id, scope, days=None, request_id=None):
    access(user_id, scope)
    stmt = select(model)
    if scope != "all":
        stmt = stmt.where(model.user_id == user_id)
    if days is not None:
        stmt = stmt.where(model.created_at >= utcnow() - timedelta(days=days))
    if request_id is not None:
        stmt = stmt.where(model.request_id == request_id)
    async with SessionLocal() as db:
        return (await db.execute(stmt.order_by(model.created_at))).scalars().all()


def llm_summary(rows):
    cost, unpriced, missing_usage = 0., 0, 0
    for r in rows:
        price = settings.observability.model_prices.get(r.model)
        if r.prompt_tokens is None or r.completion_tokens is None:
            missing_usage += 1
        elif price is None:
            unpriced += 1
        else:
            cost += (r.prompt_tokens * price.input_per_million + r.completion_tokens * price.output_per_million) / 1_000_000
    priced = len(rows) - unpriced - missing_usage
    return {"calls": len(rows), "prompt_tokens": sum(r.prompt_tokens or 0 for r in rows),
            "completion_tokens": sum(r.completion_tokens or 0 for r in rows),
            "failures": sum(r.status != "success" for r in rows), "latency_ms": latency(rows),
            "first_token_ms": latency(rows, "first_token_ms"),
            "estimated_cost": round(cost, 6) if priced else None,
            "unpriced_calls": unpriced, "missing_usage_calls": missing_usage}


def groups(rows, field):
    buckets = defaultdict(list)
    for r in rows:
        buckets[getattr(r, field)].append(r)
    return [{"name": k, **llm_summary(v)} for k, v in sorted(buckets.items())]


async def llm_stats(user_id, scope, days):
    rows = await load_rows(LLMCall, user_id, scope, days)
    tools = await load_rows(ToolCallLog, user_id, scope, days)
    daily = defaultdict(list)
    for r in rows:
        daily[r.created_at.date().isoformat()].append(r)
    today = utcnow().date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(days, -1, -1)]
    return {"scope": scope, "is_admin": access(user_id, scope), "range_days": days,
            "currency": settings.observability.currency, "cost_is_estimate": True,
            "summary": llm_summary(rows), "by_module": groups(rows, "module"), "by_model": groups(rows, "model"),
            "daily": [{"date": d, **llm_summary(daily[d])} for d in dates],
            "tools": {"calls": len(tools), "failures": sum(not t.success for t in tools),
                      "failure_rate": sum(not t.success for t in tools) / len(tools) if tools else None,
                      "latency_ms": latency(tools),
                      "by_name": [{"name": name, "calls": sum(t.tool_name == name for t in tools),
                                   "failures": sum(t.tool_name == name and not t.success for t in tools)}
                                  for name in sorted({t.tool_name for t in tools})]}}


def offline_report():
    path = resolve_path(settings.observability.offline_report_path)
    if not path.is_file():
        return {"status": "missing", "summary": {}}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        # Deliberately expose aggregate scores only, never per-case prompts or file paths.
        meta = report.get("metadata", {})
        return {"status": "available", "summary": report["summary"], "created_at": meta.get("created_at"),
                "k": meta.get("config", {}).get("k", 5)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        logger.warning("Offline retrieval report is unreadable")
        return {"status": "invalid", "summary": {}}


async def retrieval_stats(user_id, scope, days):
    rows = await load_rows(RetrievalLog, user_id, scope, days)
    return {"calls": len(rows), "degraded_count": sum(r.degraded for r in rows),
            "failures": sum(r.status != "success" for r in rows), "latency_ms": latency(rows),
            "modes": [{"name": k, "count": v} for k, v in sorted(Counter(r.mode for r in rows).items())],
            "offline": offline_report()}


def serialize(row):
    return {c.name: (value.isoformat() if hasattr(value, "isoformat") else value)
            for c in row.__table__.columns for value in [getattr(row, c.name)]}


async def requests(user_id, scope, days, limit):
    buckets = {}
    for key, model in TABLES.items():
        for r in await load_rows(model, user_id, scope, days):
            b = buckets.setdefault(r.request_id, {"request_id": r.request_id, "session_id": r.session_id,
                                  "created_at": r.created_at.isoformat(), "llm_calls": 0, "tool_calls": 0,
                                  "retrieval_logs": 0, "failures": 0})
            b[key] += 1
            b["created_at"] = min(b["created_at"], r.created_at.isoformat())
            b["failures"] += int(not r.success if key == "tool_calls" else r.status != "success")
    return sorted(buckets.values(), key=lambda b: b["created_at"], reverse=True)[:limit]


async def request_detail(user_id, scope, request_id):
    result = {key: [serialize(r) for r in await load_rows(model, user_id, scope, request_id=request_id)]
              for key, model in TABLES.items()}
    if not any(result.values()):
        raise HTTPException(404, "请求记录不存在")
    return {"request_id": request_id, **result}
