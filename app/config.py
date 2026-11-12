"""
配置加载：config.yml 提供结构化配置，.env 提供密钥。

密钥只以环境变量名的形式出现在配置里，避免把 API Key 写进仓库。
"""

from __future__ import annotations

import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger(__name__)


def resolve_path(path: str | Path) -> Path:
    """相对路径以项目根目录为基准。"""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


class LLMProfile(BaseModel):
    model: str
    base_url: str
    api_key_env: str = "LLM_API_KEY"
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout: float = 60.0
    extra_body: dict = Field(default_factory=dict)

    @property
    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "")


class LLMSettings(BaseModel):
    fast: LLMProfile
    normal: LLMProfile


class EmbeddingSettings(BaseModel):
    provider: Literal["openai", "local"] = "openai"
    model: str = "BAAI/bge-m3"
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key_env: str = "EMBEDDING_API_KEY"
    batch_size: int = 16
    timeout: float = 60.0
    query_instruction: str = ""

    @property
    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "")


class RerankerSettings(BaseModel):
    enabled: bool = False
    model: str = "BAAI/bge-reranker-v2-m3"
    base_url: str = "https://api.siliconflow.cn/v1/rerank"
    api_key_env: str = "RERANKER_API_KEY"
    candidates: int = 20
    timeout: float = 20.0

    @property
    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "")


class RetrievalSettings(BaseModel):
    dense_top_k: int = 30
    sparse_top_k: int = 30
    rrf_k: int = 60
    default_top_k: int = 6


class ContextSettings(BaseModel):
    history_token_budget: int = 8000
    compress_trigger_tokens: int = 6000
    compress_target_ratio: float = 0.5
    min_recent_turns: int = 2
    history_tool_result_tokens: int = 400
    tool_result_max_tokens: int = 2500
    summary_max_tokens: int = 800
    memory_prompt_tokens: int = 1200


class MemorySettings(BaseModel):
    auto_extract: bool = True
    temporary_ttl_days: int = 7
    max_active_in_prompt: int = 30
    max_candidates_in_prompt: int = 8
    candidate_window_days: int = 14


class AgentSettings(BaseModel):
    max_iterations: int = 8
    tool_timeout: float = 30.0
    total_timeout: float = 240.0


class DataSettings(BaseModel):
    exercises_url: str = (
        "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/dist/exercises.json"
    )
    image_base_url: str = (
        "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/"
    )
    raw_path: str = "data/raw/exercises.json"
    translation_cache_path: str = "data/exercises_zh.json"
    index_dir: str = "data/index"


class AuthSettings(BaseModel):
    jwt_secret_env: str = "JWT_SECRET"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 24 * 7

    _fallback_secret: str | None = None

    @property
    def jwt_secret(self) -> str:
        secret = os.getenv(self.jwt_secret_env, "")
        if secret:
            return secret
        # 开发环境兜底：进程内随机密钥，重启后旧 token 失效
        if self._fallback_secret is None:
            logger.warning(
                "%s 未设置，使用进程内随机密钥（重启后登录态失效）", self.jwt_secret_env
            )
            self._fallback_secret = secrets.token_urlsafe(48)
        return self._fallback_secret


class Settings(BaseModel):
    timezone: str = "Asia/Shanghai"
    cors_origins: list[str] = Field(default_factory=list)
    database_url: str
    llm: LLMSettings
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    reranker: RerankerSettings = Field(default_factory=RerankerSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    data: DataSettings = Field(default_factory=DataSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)


def _normalize_db_url(url: str) -> str:
    """把 sqlite 相对路径转换为基于项目根目录的绝对路径。"""
    prefix = "sqlite+aiosqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix):]
        if path and path != ":memory:" and not Path(path).is_absolute():
            return prefix + str(PROJECT_ROOT / path)
    return url


@lru_cache
def get_settings() -> Settings:
    config_path = Path(os.getenv("FITNESSPRO_CONFIG", PROJECT_ROOT / "config.yml"))
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    db_url = (
        os.getenv("DATABASE_URL")
        or (raw.pop("database", None) or {}).get("url")
        or "sqlite+aiosqlite:///data/fitnesspro.db"
    )
    raw.pop("database", None)
    raw["database_url"] = _normalize_db_url(db_url)
    return Settings.model_validate(raw)


settings = get_settings()
