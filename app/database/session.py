from pathlib import Path

from sqlalchemy import event, inspect, text
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
    """创建表结构。正式环境建议改用 Alembic 迁移。"""
    from app.database import models  # noqa: F401  注册模型

    if engine.dialect.name == "sqlite" and engine.url.database:
        Path(engine.url.database).parent.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Additive upgrade for existing local databases; all original profile data is retained.
        columns = await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns("user_profiles")})
        for name, sql_type in (("training_location", "VARCHAR(24)"), ("excluded_equipment", "JSON")):
            if name not in columns:
                await conn.execute(text(f"ALTER TABLE user_profiles ADD COLUMN {name} {sql_type}"))
