"""
配置加载：config.yml 提供结构化配置，.env 提供密钥。

密钥只以环境变量名的形式出现在配置里，避免把 API Key 写进仓库。
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


def bundle_root() -> Path:
    """只读资源目录：开发时是项目根，安装包里是随程序打包的目录。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[1]


def writable_root() -> Path:
    """数据库、密钥和日志。安装版写到本机用户目录，避免装到 Program Files 后无法保存。"""
    if getattr(sys, "frozen", False):
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "FitnessPro"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return bundle_root()


PROJECT_ROOT = bundle_root()
load_dotenv(writable_root() / ".env")
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger(__name__)


def resolve_path(path: str | Path) -> Path:
    """相对路径以项目根目录为基准。安装版里 data/ 改写到用户目录。"""
    p = Path(path)
    if p.is_absolute():
        return p
    text = p.as_posix()
    base = writable_root() if text == "data" or text.startswith("data/") else bundle_root()
    return base / p


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
    vision: LLMProfile | None = None


class VisionSettings(BaseModel):
    enabled: bool = False
    upload_dir: str = "data/uploads/equipment"
    max_upload_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    max_pixels: int = Field(default=20_000_000, ge=1_000_000, le=40_000_000)
    max_edge: int = Field(default=1600, ge=512, le=2048)


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


class RetrievalCacheSettings(BaseModel):
    enabled: bool = True
    l1_ttl_seconds: int = Field(default=3600, gt=0)
    l2_enabled: bool = True
    l2_ttl_seconds: int = Field(default=3600, gt=0)
    l2_similarity_threshold: float = Field(default=0.97, ge=0, le=1)
    timeout_seconds: float = Field(default=0.3, gt=0)


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
    max_iterations: int = 12
    tool_timeout: float = 30.0
    total_timeout: float = Field(default=600.0, gt=0, allow_inf_nan=False)
    final_answer_reserve: float = 60.0


class LimitSettings(BaseModel):
    chat_requests: int = Field(default=10, gt=0)
    chat_window_seconds: int = Field(default=60, gt=0)
    daily_llm_tokens: int = Field(default=100_000, gt=0)
    login_failures: int = Field(default=5, gt=0)
    login_window_seconds: int = Field(default=900, gt=0)


class EvaluationSettings(BaseModel):
    """RAGAS 在线评估：回答完成后在后台对检索类回答打分，不阻塞对话。"""

    enabled: bool = False
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    llm_tier: Literal["fast", "normal"] = "fast"
    timeout_seconds: float = 300.0
    max_concurrency: int = 2
    max_context_chars: int = 12000
    # 评审模型的额外请求参数。DeepSeek 思考模式会先生成上万字推理，容易撞 max_tokens 导致
    # 结构化输出被截断并反复重试，评估应关闭思考：{"thinking": {"type": "disabled"}}
    llm_extra_body: dict = Field(default_factory=dict)
    llm_max_tokens: int = 8192
    # 无参考答案时只有这两个指标可用
    metrics: list[Literal["faithfulness", "answer_relevancy"]] = Field(
        default_factory=lambda: ["faithfulness", "answer_relevancy"]
    )
    # 低于阈值的评估会输出 warning 日志
    alert_faithfulness: float = 0.3
    alert_answer_relevancy: float = 0.5


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


class ModelPrice(BaseModel):
    # Currency units per million tokens; unset models are explicitly unpriced.
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)


class ObservabilitySettings(BaseModel):
    admin_user_ids: list[str] = Field(default_factory=list)
    currency: str = "CNY"
    model_prices: dict[str, ModelPrice] = Field(default_factory=dict)
    offline_report_path: str = "eval/results/retrieval/retrieval_report.json"


class WebSearchSettings(BaseModel):
    enabled: bool = False
    api_key_env: str = "LLM_API_KEY"
    timeout: float = Field(default=15.0, gt=0, le=30)

    @property
    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "")


class KnowledgeSettings(BaseModel):
    raw_dir: str = "data/knowledge/raw"
    index_dir: str = "data/knowledge/index"
    chunk_size: int = Field(default=900, ge=100, le=2000)


class NutritionSettings(BaseModel):
    source_file: str = "meals.json"
    index_dir: str = "data/nutrition/index"


class VectorStoreSettings(BaseModel):
    type: Literal["local", "qdrant"] = "local"
    url: str = "http://localhost:6333"
    collection_prefix: str = "fitnesspro"
    timeout: float = 30.0


class Settings(BaseModel):
    timezone: str = "Asia/Shanghai"
    cors_origins: list[str] = Field(default_factory=list)
    database_url: str
    vector_store: VectorStoreSettings = Field(default_factory=VectorStoreSettings)
    llm: LLMSettings
    vision: VisionSettings = Field(default_factory=VisionSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    reranker: RerankerSettings = Field(default_factory=RerankerSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    retrieval_cache: RetrievalCacheSettings = Field(default_factory=RetrievalCacheSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    web_search: WebSearchSettings = Field(default_factory=WebSearchSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    evaluation: EvaluationSettings = Field(default_factory=EvaluationSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    nutrition: NutritionSettings = Field(default_factory=NutritionSettings)
    data: DataSettings = Field(default_factory=DataSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)


def _normalize_db_url(url: str) -> str:
    """把 sqlite 相对路径转换为基于项目根目录的绝对路径。"""
    prefix = "sqlite+aiosqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix):]
        if path and path != ":memory:" and not Path(path).is_absolute():
            return prefix + str(resolve_path(path))
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


def load_runtime_overrides() -> None:
    # 延后导入，避免和配置模块循环依赖。界面保存的密钥在此时覆盖环境变量。
    from app.runtime_config import apply_stored_settings

    apply_stored_settings()


load_runtime_overrides()
