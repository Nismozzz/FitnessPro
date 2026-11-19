"""FastAPI 入口：uvicorn app.main:app --reload"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from app.training.service import TrainingError
from fastapi.middleware.cors import CORSMiddleware

from app.api import agent, auth, knowledge, memories, settings as provider_settings, users, training
from app.config import bundle_root, settings
from app.database.session import engine, init_db
from app.rag.retriever import init_retriever
from app.utils.background import drain

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    await init_retriever()
    yield
    await drain()
    await engine.dispose()


app = FastAPI(title="FitnessPro", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api = APIRouter(prefix="/api/v1")
for module in (auth, users, memories, agent, knowledge, training, provider_settings):
    api.include_router(module.router)
app.include_router(api)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(TrainingError)
async def training_error(_request, exc: TrainingError):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc), "validation": exc.validation})


_DIST = bundle_root() / "frontend" / "dist"
if (_DIST / "index.html").is_file():
    # 生产构建挂在 API 之后，浏览器打开 http://127.0.0.1:8000 即可使用
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
