"""FastAPI 入口：uvicorn app.main:app --reload"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from app.training.service import TrainingError
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from app.api import agent, auth, evaluations, knowledge, memories, settings as provider_settings, users, training, observability, images
from app.config import bundle_root, settings
from app.database.session import engine, init_db
from app.agent.service import recover_interrupted_turns
from app.rag.retriever import init_retriever
from app.knowledge.retriever import init_article_retriever
from app.nutrition.retriever import init_meal_retriever
from app.utils.background import drain
from app.observability.middleware import RequestTraceMiddleware
from app.request_limits import RequestLimits
from app.rag import cache as retrieval_cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    url = os.getenv("REDIS_URL")
    client = None
    if url:
        client = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2, retry=Retry(NoBackoff(), 0))
    limits = RequestLimits(client)
    cache_cfg = settings.retrieval_cache
    qdrant = None
    if cache_cfg.enabled and cache_cfg.l2_enabled and settings.vector_store.type == "qdrant":
        qdrant = httpx.AsyncClient(base_url=settings.vector_store.url.rstrip("/"), timeout=cache_cfg.timeout_seconds)
    previous_cache = retrieval_cache.cache
    retrieval_cache.cache = retrieval_cache.RetrievalCache(client, qdrant)
    previous_limits = app.state.request_limits
    app.state.request_limits = limits
    try:
        if client is not None:
            await client.ping()
        await init_db()
        await recover_interrupted_turns(limits)
        await init_retriever()
        init_article_retriever()
        init_meal_retriever()
        try:
            yield
        finally:
            await drain()
    finally:
        app.state.request_limits = previous_limits
        retrieval_cache.cache = previous_cache
        if qdrant is not None:
            await qdrant.aclose()
        await limits.close()
        await engine.dispose()


app = FastAPI(title="FitnessPro", version="0.1.0", lifespan=lifespan)
app.state.request_limits = RequestLimits()
app.add_middleware(RequestTraceMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "Retry-After"],
)

api = APIRouter(prefix="/api/v1")
for module in (auth, users, memories, agent, knowledge, training, evaluations, observability, images, provider_settings):
    api.include_router(module.router)
app.include_router(api)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(RedisError)
async def redis_error(_request, exc: RedisError):
    logging.getLogger(__name__).warning("Redis 请求失败", exc_info=exc)
    return JSONResponse(status_code=503, content={"detail": "限流服务暂不可用，请稍后再试"})


@app.exception_handler(TrainingError)
async def training_error(_request, exc: TrainingError):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc), "validation": exc.validation})


_DIST = bundle_root() / "frontend" / "dist"
if (_DIST / "index.html").is_file():
    # 生产构建挂在 API 之后，浏览器打开 http://127.0.0.1:8000 即可使用
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
