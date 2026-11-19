from pathlib import Path

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, echo=False)

if engine.dialect.name == "sqlite":

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - 连接级配置
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """通过版本化迁移初始化/升级；已有无版本数据库需显式接管。"""
    from alembic import command
    from alembic.config import Config
    from app.config import PROJECT_ROOT

    if engine.dialect.name == "sqlite" and engine.url.database:
        Path(engine.url.database).parent.mkdir(parents=True, exist_ok=True)

    def migrate(connection):
        tables = set(inspect(connection).get_table_names())
        if tables and "alembic_version" not in tables:
            raise RuntimeError(
                "检测到无版本记录的旧数据库；请先备份，再运行 "
                "python -m scripts.adopt_legacy_db，检查通过后重启。"
            )
        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")

    async with engine.begin() as conn:
        await conn.run_sync(migrate)
