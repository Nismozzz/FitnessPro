"""Back up first; explicitly adopt a pre-Alembic database after schema validation."""
import asyncio

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Column, JSON, String, inspect
from alembic.operations import Operations

from app.config import PROJECT_ROOT
from app.database import models  # noqa: F401
from app.database.session import Base, engine


def adopt(connection):
    inspector = inspect(connection)
    if 'alembic_version' in inspector.get_table_names():
        raise RuntimeError('数据库已有版本记录，请使用 alembic upgrade head')
    if set(inspector.get_table_names()) != set(Base.metadata.tables):
        raise RuntimeError('旧数据库表集合与当前基线不一致，停止接管，请人工编写迁移')
    columns = {column['name'] for column in inspector.get_columns('user_profiles')}
    operations = Operations(MigrationContext.configure(connection))
    for name, type_ in [('training_location', String(24)), ('excluded_equipment', JSON())]:
        if name not in columns:
            operations.add_column('user_profiles', Column(name, type_, nullable=True))
    differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    if differences:
        raise RuntimeError(f'旧数据库结构与基线不一致，停止接管：{differences}')
    cfg = Config(str(PROJECT_ROOT / 'alembic.ini'))
    cfg.attributes['connection'] = connection
    command.stamp(cfg, '0001')


async def main():
    try:
        async with engine.begin() as connection:
            await connection.run_sync(adopt)
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
