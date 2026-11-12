"""FastAPI 入口：uvicorn app.main:app --reload"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import agent, auth, knowledge, memories, users
from app.config import settings
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
for module in (auth, users, memories, agent, knowledge):
    api.include_router(module.router)
app.include_router(api)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
