"""测试环境：使用临时 SQLite 数据库，必须在导入 app 之前设置。"""

import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="fitnesspro-test-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp / 'test.db'}"
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-1234")
os.environ.setdefault("LLM_API_KEY", "test")
os.environ.setdefault("FAST_LLM_API_KEY", "test")

import pytest  # noqa: E402

from app.database.session import init_db  # noqa: E402
from app.config import settings  # noqa: E402

# Unit tests use isolated local indexes; Qdrant has dedicated integration tests.
settings.vector_store.type = "local"


@pytest.fixture
async def db():
    await init_db()
    yield
